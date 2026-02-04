"""Index API endpoints for file scanning and querying."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Literal
from datetime import datetime
from pathlib import Path
import json

from app.services.indexer import IndexerService
from app.services.differ import compute_diff, DiffEntry
from app.services.safetensors import (
    read_safetensors_header,
    SafetensorsHeaderError,
    classify_safetensors_header,
)
from app.config import get_settings
from app.database import get_db
from starlette.concurrency import run_in_threadpool

router = APIRouter()


class RefreshRequest(BaseModel):
    side: Literal["local", "lake", "both"] = "both"


@router.get("/config")
async def get_config():
    """Get frontend configuration."""
    from app.config import get_settings
    settings = get_settings()
    return {
        "local_allow_delete": settings.local_allow_delete,
        "lake_allow_delete": settings.lake_allow_delete,
    }


class RefreshResponse(BaseModel):
    side: str
    files_indexed: int
    duration_ms: float


class FileEntry(BaseModel):
    relpath: str
    size: int
    mtime_ns: int
    hash: str | None
    side: str


@router.post("/refresh", response_model=list[RefreshResponse])
async def refresh_index(request: RefreshRequest):
    """
    Refresh the file index for one or both sides.
    Walks the filesystem and updates the database.
    """
    indexer = IndexerService()
    results = []
    
    sides = ["local", "lake"] if request.side == "both" else [request.side]
    
    for side in sides:
        start = datetime.now()
        count = await indexer.scan_side(side)  # type: ignore
        duration = (datetime.now() - start).total_seconds() * 1000
        results.append(RefreshResponse(
            side=side,
            files_indexed=count,
            duration_ms=round(duration, 2)
        ))
    
    return results


@router.get("/files", response_model=list[FileEntry])
async def get_files(
    side: Literal["local", "lake"],
    folder: str = "",
    query: str = "",
):
    """
    Get files from the index.
    - folder: filter to files within this folder (relpath prefix)
    - query: fuzzy search filter on filename
    """
    indexer = IndexerService()
    files = await indexer.get_files(side, folder=folder, query=query)
    return files


@router.get("/folders")
async def get_folders(
    side: Literal["local", "lake"],
    parent: str = "",
):
    """
    Get immediate subfolders under a parent folder.
    """
    indexer = IndexerService()
    folders = await indexer.get_folders(side, parent=parent)
    return {"folders": folders}


@router.get("/diff", response_model=list[DiffEntry])
async def get_diff(
    folder: str = "",
    query: str = "",
):
    """
    Get diff between Local and Lake.
    Returns entries with their diff status.
    """
    diff = await compute_diff(folder=folder, query=query)
    return diff


@router.get("/stats")
async def get_stats():
    """Get index statistics for both sides."""
    indexer = IndexerService()
    local_stats = await indexer.get_stats("local")
    lake_stats = await indexer.get_stats("lake")
    return {
        "local": local_stats,
        "lake": lake_stats,
    }


def _resolve_safetensors_path(relpath: str, side: Literal["local", "lake", "auto"]):
    relpath = relpath.strip()
    if not relpath:
        raise HTTPException(status_code=400, detail="relpath is required")
    if ".." in relpath or relpath.startswith("/") or "\\" in relpath:
        raise HTTPException(status_code=400, detail="Invalid relpath")
    if not relpath.lower().endswith(".safetensors"):
        raise HTTPException(status_code=400, detail="Not a .safetensors file")

    settings = get_settings()
    roots: list[tuple[str, Path]]
    if side == "local":
        roots = [("local", settings.local_models_root)]
    elif side == "lake":
        roots = [("lake", settings.lake_models_root)]
    else:
        roots = [("local", settings.local_models_root), ("lake", settings.lake_models_root)]

    chosen_side = None
    file_path = None
    for side_name, root in roots:
        candidate = (root / relpath).resolve()
        if not str(candidate).startswith(str(root.resolve())):
            continue
        if candidate.exists() and candidate.is_file():
            chosen_side = side_name
            file_path = candidate
            break

    if not file_path:
        raise HTTPException(status_code=404, detail="File not found")

    return chosen_side, file_path


@router.get("/safetensors/header")
async def get_safetensors_header(
    relpath: str = Query(...),
    side: Literal["local", "lake", "auto"] = "auto",
):
    """
    Read the JSON header from a .safetensors file.
    """
    chosen_side, file_path = _resolve_safetensors_path(relpath, side)

    try:
        header = read_safetensors_header(file_path)
    except SafetensorsHeaderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read header: {exc}")

    return {
        "relpath": relpath,
        "side": chosen_side,
        "header": header,
    }


@router.get("/safetensors/classify")
async def classify_safetensors(
    relpath: str = Query(...),
    side: Literal["local", "lake", "auto"] = "auto",
    force: bool = False,
):
    """
    Classify a .safetensors file using header heuristics.
    """
    chosen_side, file_path = _resolve_safetensors_path(relpath, side)

    try:
        stat = file_path.stat()
        cache_key = f"{chosen_side}:{relpath}"

        if not force:
            async with get_db() as db:
                cursor = await db.execute(
                    "SELECT size, mtime_ns, payload_json FROM safetensors_cache WHERE key = ?",
                    (cache_key,),
                )
                row = await cursor.fetchone()
                if row and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
                    payload = json.loads(row["payload_json"])
                    return {
                        "relpath": relpath,
                        "side": chosen_side,
                        **payload,
                    }

        header = await run_in_threadpool(read_safetensors_header, file_path)
        result = classify_safetensors_header(header, relpath=relpath)
        payload = {
            "tags": result.get("tags", []),
            "confidence": result.get("confidence", 0.0),
            "signals": result.get("signals", []),
            "signals_by_tag": result.get("signals_by_tag", {}),
        }

        async with get_db() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO safetensors_cache
                (key, side, relpath, size, mtime_ns, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    chosen_side,
                    relpath,
                    stat.st_size,
                    stat.st_mtime_ns,
                    json.dumps(payload),
                    datetime.now().isoformat(),
                ),
            )
            await db.commit()
    except SafetensorsHeaderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to classify header: {exc}")

    return {
        "relpath": relpath,
        "side": chosen_side,
        **payload,
    }


@router.post("/safetensors/reclassify")
async def reclassify_safetensors_all():
    """
    Recalculate classifications for all indexed .safetensors files.
    """
    settings = get_settings()
    total = 0
    updated = 0
    errors = 0

    async with get_db() as db:
        await db.execute("DELETE FROM safetensors_cache")
        await db.commit()
        cursor = await db.execute(
            "SELECT side, relpath FROM file_index WHERE relpath LIKE '%.safetensors'"
        )
        rows = await cursor.fetchall()

    for row in rows:
        side_name = row["side"]
        relpath = row["relpath"]
        total += 1

        root = settings.local_models_root if side_name == "local" else settings.lake_models_root
        file_path = (root / relpath).resolve()
        if not file_path.exists():
            errors += 1
            continue

        try:
            stat = file_path.stat()
            header = await run_in_threadpool(read_safetensors_header, file_path)
            result = classify_safetensors_header(header, relpath=relpath)
            payload = {
                "tags": result.get("tags", []),
                "confidence": result.get("confidence", 0.0),
                "signals": result.get("signals", []),
                "signals_by_tag": result.get("signals_by_tag", {}),
            }
            cache_key = f"{side_name}:{relpath}"
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO safetensors_cache
                    (key, side, relpath, size, mtime_ns, payload_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cache_key,
                        side_name,
                        relpath,
                        stat.st_size,
                        stat.st_mtime_ns,
                        json.dumps(payload),
                        datetime.now().isoformat(),
                    ),
                )
                await db.commit()
            updated += 1
        except Exception:
            errors += 1
            continue

    return {
        "status": "completed",
        "total": total,
        "updated": updated,
        "errors": errors,
    }



class VerifyRequest(BaseModel):
    folder: str = ""  # If empty, verify specific file
    relpath: str = ""  # Specific file to verify


@router.post("/verify")
async def verify_hashes(request: VerifyRequest):
    """
    Queue a verification task.
    """
    from datetime import datetime, timezone
    from app.database import get_db
    from app.websocket import broadcast

    task_type = "verify"
    
    # Check if a similar task is already pending/running
    # (Simple check to avoid duplicate clicks, though queue could handle strict deduping)
    
    async with get_db() as db:
        if request.relpath:
             # Check for existing verify task for this file
             sql = "SELECT id FROM queue WHERE task_type='verify' AND src_relpath=? AND status IN ('pending', 'running')"
             cursor = await db.execute(sql, (request.relpath,))
             if await cursor.fetchone():
                 return {"status": "queued", "message": "Verification already queued for this file"}
                 
             # Enqueue
             await db.execute(
                """
                INSERT INTO queue (task_type, src_relpath, created_at, size_bytes)
                VALUES (?, ?, ?, 0)
                """,
                ("verify", request.relpath, datetime.now(timezone.utc).isoformat())
            )
             
        else:
             # Folder or all
             folder = request.folder or ""
             sql = "SELECT id FROM queue WHERE task_type='verify' AND verify_folder=? AND status IN ('pending', 'running')"
             cursor = await db.execute(sql, (folder,))
             if await cursor.fetchone():
                 return {"status": "queued", "message": "Verification already queued for this folder"}
             
             # Enqueue
             await db.execute(
                """
                INSERT INTO queue (task_type, verify_folder, created_at, size_bytes)
                VALUES (?, ?, ?, 0)
                """,
                ("verify", folder, datetime.now(timezone.utc).isoformat())
            )
            
        await db.commit()
        
    # Trigger worker check via broadcast (or worker polling picks it up)
    # The worker polls, so it will pick it up automatically
    
    return {"status": "queued"}
