"""Index API endpoints for file scanning and querying."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Literal
from datetime import datetime

from app.services.indexer import IndexerService
from app.services.differ import compute_diff, DiffEntry

router = APIRouter()


class RefreshRequest(BaseModel):
    side: Literal["local", "lake", "both"] = "both"


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
