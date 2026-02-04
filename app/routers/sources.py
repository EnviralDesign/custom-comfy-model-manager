"""API Router for Source URL Management (Hash -> URL metadata)."""

from datetime import datetime, timezone
import json
from urllib.parse import urlparse, unquote
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Any

from app.config import get_settings
from app.services.source_manager import get_source_manager, ModelSource
from app.database import get_db

from starlette.concurrency import run_in_threadpool
import requests

router = APIRouter()


def check_url_sync(url: str) -> dict:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.head(url, allow_redirects=True, timeout=10, headers=headers)

        # If 404 or other error, or if Content-Length is missing (some sites block HEAD), try GET
        if response.status_code != 200 or not response.headers.get("Content-Length"):
            response = requests.get(url, stream=True, timeout=10, headers=headers)

        size = response.headers.get("Content-Length")
        content_type = response.headers.get("Content-Type", "").lower()

        # Heuristic: if it's text/html, it's likely a landing page, not a direct download
        is_webpage = "text/html" in content_type

        return {
            "ok": response.status_code == 200 and not is_webpage,
            "status": response.status_code,
            "size": int(size) if size else None,
            "type": content_type,
            "url": response.url,
            "is_webpage": is_webpage,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _extract_response_text(payload: dict) -> str:
    if not payload:
        return ""
    if isinstance(payload.get("output_text"), str):
        return payload.get("output_text", "")

    # OpenAI-style Responses API output
    output = payload.get("output", [])
    if isinstance(output, list):
        text_parts = []
        for item in output:
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                part_type = part.get("type")
                if part_type in ("output_text", "text"):
                    text_parts.append(part.get("text", ""))
        if text_parts:
            return "\n".join(text_parts)

    # Chat completions fallback
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            return message.get("content", "")

    return ""


def _extract_json_object(text: str) -> Optional[dict]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = cleaned[start:end + 1]
            try:
                return json.loads(snippet)
            except Exception:
                return None
    return None


def _url_basename(url: str) -> str:
    try:
        path = urlparse(url).path
        return unquote(path.rsplit("/", 1)[-1])
    except Exception:
        return ""


def _filename_matches_url(filename: str, url: str) -> bool:
    if not filename or not url:
        return False
    return _url_basename(url) == filename


@router.get("/check-url")
async def check_url(url: str):
    """
    Check if a URL is valid and reachable.
    Returns status code and file size if available.
    """
    return await run_in_threadpool(check_url_sync, url)


class SourceURLRequest(BaseModel):
    url: str
    notes: Optional[str] = None
    filename_hint: Optional[str] = None
    queue_hash: bool = False  # If true, queue a hash task for this file


class SourceURLResponse(BaseModel):
    key: str  # hash or relpath:xxx
    url: str
    added_at: str
    notes: Optional[str] = None
    filename_hint: Optional[str] = None
    relpath: Optional[str] = None  # Set if this is a relpath-based entry


class AiSourceLookupRequest(BaseModel):
    filename: str
    relpath: Optional[str] = None


class AiSourceLookupResponse(BaseModel):
    found: bool
    accepted: bool
    url: Optional[str] = None
    filename: str
    reason: Optional[str] = None
    validation: Optional[dict[str, Any]] = None
    model: Optional[str] = None


@router.post("/sources/ai-lookup", response_model=AiSourceLookupResponse)
async def ai_lookup_source_url(request: AiSourceLookupRequest):
    """
    Use xAI Grok (with web search) to find a direct download URL for an exact filename.
    """
    settings = get_settings()
    api_key = settings.xai_api_key
    if not api_key:
        raise HTTPException(status_code=400, detail="XAI_API_KEY is not configured")

    filename = request.filename.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")

    def _lookup() -> AiSourceLookupResponse:
        model = settings.xai_model
        base_url = settings.xai_api_base_url.rstrip("/")

        system_prompt = (
            "You are a web research assistant. Your task is to find a public direct download URL "
            "for the exact file name provided. Start with Hugging Face and Civitai, but if no exact "
            "match is found you may search elsewhere. Only return a URL if the filename matches "
            "exactly (case-sensitive) and points to a direct file download (not a landing page). "
            "If you cannot find an exact match, return found=false and url=null. "
            "Return ONLY a JSON object with keys: found (boolean), url (string|null), source (string|null), notes (string|null)."
        )
        user_prompt = f"File name: {filename}\nPath: {request.relpath or ''}"

        payload = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "tools": [{"type": "web_search"}],
            "temperature": 0.2,
            "max_output_tokens": 500,
            "store": False,
        }

        try:
            response = requests.post(
                f"{base_url}/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=60,
            )
        except Exception as exc:
            return AiSourceLookupResponse(
                found=False,
                accepted=False,
                url=None,
                filename=filename,
                reason=f"xAI request failed: {exc}",
                model=model,
            )

        if response.status_code >= 400:
            return AiSourceLookupResponse(
                found=False,
                accepted=False,
                url=None,
                filename=filename,
                reason=f"xAI error {response.status_code}: {response.text}",
                model=model,
            )

        data = response.json()
        text = _extract_response_text(data)
        result = _extract_json_object(text) or {}
        candidate_url = (result.get("url") or "").strip()
        found = bool(result.get("found")) and bool(candidate_url)

        if not found:
            return AiSourceLookupResponse(
                found=False,
                accepted=False,
                url=None,
                filename=filename,
                reason="No exact filename match found",
                model=model,
            )

        if not _filename_matches_url(filename, candidate_url):
            return AiSourceLookupResponse(
                found=False,
                accepted=False,
                url=None,
                filename=filename,
                reason="Candidate URL filename does not match exactly",
                model=model,
            )

        validation = check_url_sync(candidate_url)
        if not validation.get("ok"):
            return AiSourceLookupResponse(
                found=True,
                accepted=False,
                url=None,
                filename=filename,
                reason="Candidate URL failed validation",
                validation=validation,
                model=model,
            )

        return AiSourceLookupResponse(
            found=True,
            accepted=True,
            url=candidate_url,
            filename=filename,
            validation=validation,
            model=model,
        )

    return await run_in_threadpool(_lookup)


@router.get("/sources/{file_hash}", response_model=SourceURLResponse | None)
async def get_source_url(file_hash: str):
    """
    Get the source URL for a given file hash.
    Returns null if no source URL is set.
    """
    source_mgr = get_source_manager()
    source = await source_mgr.get_source(file_hash)
    
    if not source:
        return None
    
    return SourceURLResponse(
        key=file_hash,
        url=source.url,
        added_at=source.added_at,
        notes=source.notes,
        filename_hint=source.filename_hint,
        relpath=source.relpath,
    )


@router.get("/sources/by-relpath/{relpath:path}", response_model=SourceURLResponse | None)
async def get_source_url_by_relpath(relpath: str):
    """
    Get the source URL for a file by relpath (for unhashed files).
    """
    source_mgr = get_source_manager()
    result = await source_mgr.get_source_by_relpath(relpath)
    
    if not result:
        return None
    
    key, source = result
    return SourceURLResponse(
        key=key,
        url=source.url,
        added_at=source.added_at,
        notes=source.notes,
        filename_hint=source.filename_hint,
        relpath=source.relpath,
    )


@router.put("/sources/{file_hash}", response_model=SourceURLResponse)
async def set_source_url(file_hash: str, request: SourceURLRequest):
    """
    Set or update the source URL for a given file hash.
    """
    if not request.url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty")
    
    source_mgr = get_source_manager()
    
    source = ModelSource(
        url=request.url.strip(),
        added_at=datetime.now(timezone.utc).isoformat(),
        notes=request.notes,
        filename_hint=request.filename_hint,
    )
    
    await source_mgr.set_source(file_hash, source)
    
    return SourceURLResponse(
        key=file_hash,
        url=source.url,
        added_at=source.added_at,
        notes=source.notes,
        filename_hint=source.filename_hint,
    )


@router.put("/sources/by-relpath/{relpath:path}", response_model=SourceURLResponse)
async def set_source_url_by_relpath(relpath: str, request: SourceURLRequest):
    """
    Set or update the source URL for a file by relpath (for unhashed files).
    Optionally queues a hash task.
    """
    if not request.url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty")
    
    source_mgr = get_source_manager()
    
    source = ModelSource(
        url=request.url.strip(),
        added_at=datetime.now(timezone.utc).isoformat(),
        notes=request.notes,
        filename_hint=request.filename_hint,
        relpath=relpath,
    )
    
    await source_mgr.set_source_by_relpath(relpath, source)
    
    # Queue hash if requested
    if request.queue_hash:
        async with get_db() as db:
            # Check if already queued
            cursor = await db.execute(
                "SELECT id FROM queue WHERE task_type='hash_file' AND src_relpath=? AND status IN ('pending', 'running')",
                (relpath,)
            )
            if not await cursor.fetchone():
                await db.execute(
                    """
                    INSERT INTO queue (task_type, src_relpath, created_at, size_bytes)
                    VALUES (?, ?, ?, 0)
                    """,
                    ("hash_file", relpath, datetime.now(timezone.utc).isoformat())
                )
                await db.commit()
    
    return SourceURLResponse(
        key=f"relpath:{relpath}",
        url=source.url,
        added_at=source.added_at,
        notes=source.notes,
        filename_hint=source.filename_hint,
        relpath=relpath,
    )


@router.delete("/sources/{file_hash}")
async def delete_source_url(file_hash: str):
    """
    Remove the source URL for a given file hash.
    """
    source_mgr = get_source_manager()
    
    # Check if it exists
    existing = await source_mgr.get_source(file_hash)
    if not existing:
        raise HTTPException(status_code=404, detail="Source URL not found for this hash")
    
    await source_mgr.remove_source(file_hash)
    
    return {"status": "deleted", "key": file_hash}


@router.delete("/sources/by-relpath/{relpath:path}")
async def delete_source_url_by_relpath(relpath: str):
    """
    Remove the source URL for a file by relpath.
    """
    source_mgr = get_source_manager()
    
    result = await source_mgr.get_source_by_relpath(relpath)
    if not result:
        raise HTTPException(status_code=404, detail="Source URL not found for this relpath")
    
    await source_mgr.remove_source_by_relpath(relpath)
    
    return {"status": "deleted", "relpath": relpath}


@router.get("/sources")
async def list_all_sources():
    """
    List all source URLs.
    Useful for debugging and overview.
    """
    source_mgr = get_source_manager()
    all_sources = await source_mgr.get_all_sources()
    
    return {
        "count": len(all_sources),
        "sources": [
            {
                "key": k,
                "url": s.url,
                "added_at": s.added_at,
                "notes": s.notes,
                "filename_hint": s.filename_hint,
                "relpath": s.relpath,
            }
            for k, s in all_sources.items()
        ]
    }


@router.post("/hash-file")
async def queue_hash_file(relpath: str):
    """
    Queue a hash task for a single file by relpath.
    """
    async with get_db() as db:
        # Check if already queued
        cursor = await db.execute(
            "SELECT id FROM queue WHERE task_type='hash_file' AND src_relpath=? AND status IN ('pending', 'running')",
            (relpath,)
        )
        if await cursor.fetchone():
            return {"status": "already_queued", "relpath": relpath}
        
        await db.execute(
            """
            INSERT INTO queue (task_type, src_relpath, created_at, size_bytes)
            VALUES (?, ?, ?, 0)
            """,
            ("hash_file", relpath, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    
    return {"status": "queued", "relpath": relpath}
