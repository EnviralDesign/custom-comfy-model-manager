"""API Router for Source URL Management (Hash -> URL metadata)."""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Any

from app.config import get_settings
from app.services.source_manager import get_source_manager, ModelSource
from app.database import get_db
from app.services.ai_lookup_service import call_ai_lookup
from app.services.url_utils import check_url_sync, filename_matches_url

from starlette.concurrency import run_in_threadpool

router = APIRouter()


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
    api_key = settings.xai_api_key or ""

    filename = request.filename.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")

    def _lookup() -> AiSourceLookupResponse:
        model = settings.xai_model
        base_url = settings.xai_api_base_url.rstrip("/")

        try:
            result = call_ai_lookup(
                base_url=base_url,
                api_key=api_key,
                model=model,
                filename=filename,
                relpath=request.relpath,
                file_hash=None,
                civitai_base_url=settings.civitai_api_base_url,
                civitai_api_key=settings.civitai_api_key,
                huggingface_api_key=settings.huggingface_api_key,
                lookup_mode=settings.ai_lookup_mode,
                tool_max_steps=settings.ai_tool_max_steps,
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

        if result.get("error"):
            return AiSourceLookupResponse(
                found=False,
                accepted=False,
                url=None,
                filename=filename,
                reason=result["error"],
                model=model,
            )

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

        if not filename_matches_url(filename, candidate_url, validation.get("filename")):
            return AiSourceLookupResponse(
                found=False,
                accepted=False,
                url=None,
                filename=filename,
                reason="Candidate URL filename does not match exactly",
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
