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
