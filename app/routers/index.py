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


class VerifyResponse(BaseModel):
    verified: int
    matched: int
    mismatched: int
    duration_ms: float


@router.post("/verify", response_model=VerifyResponse)
async def verify_hashes(request: VerifyRequest):
    """
    Compute and compare hashes for files that exist on both sides.
    If folder is provided, verifies all probable_same files in that folder.
    If relpath is provided, verifies that specific file.
    """
    import blake3
    import aiofiles
    from pathlib import Path
    from app.config import get_settings
    from app.database import get_db
    
    settings = get_settings()
    start = datetime.now()
    verified = 0
    matched = 0
    mismatched = 0
    
    async with get_db() as db:
        # Find files that need verification (exist on both sides, same size, missing hash on either side)
        if request.relpath:
            # Verify specific file
            sql = """
                SELECT l.relpath, l.size, l.hash as local_hash, r.hash as lake_hash
                FROM file_index l
                JOIN file_index r ON l.relpath = r.relpath AND l.size = r.size
                WHERE l.side = 'local' AND r.side = 'lake'
                AND l.relpath = ?
                AND (l.hash IS NULL OR r.hash IS NULL)
            """
            cursor = await db.execute(sql, (request.relpath,))
        else:
            # Verify folder
            folder_prefix = request.folder.replace("\\", "/").strip("/")
            if folder_prefix:
                sql = """
                    SELECT l.relpath, l.size, l.hash as local_hash, r.hash as lake_hash
                    FROM file_index l
                    JOIN file_index r ON l.relpath = r.relpath AND l.size = r.size
                    WHERE l.side = 'local' AND r.side = 'lake'
                    AND l.relpath LIKE ?
                    AND (l.hash IS NULL OR r.hash IS NULL)
                """
                cursor = await db.execute(sql, (f"{folder_prefix}/%",))
            else:
                # All files
                sql = """
                    SELECT l.relpath, l.size, l.hash as local_hash, r.hash as lake_hash
                    FROM file_index l
                    JOIN file_index r ON l.relpath = r.relpath AND l.size = r.size
                    WHERE l.side = 'local' AND r.side = 'lake'
                    AND (l.hash IS NULL OR r.hash IS NULL)
                """
                cursor = await db.execute(sql)
        
        files = await cursor.fetchall()
        
        for row in files:
            relpath = row["relpath"]
            local_path = settings.local_models_root / relpath.replace("/", "\\")
            lake_path = settings.lake_models_root / relpath.replace("/", "\\")
            
            try:
                # Compute hashes if needed
                local_hash = row["local_hash"]
                lake_hash = row["lake_hash"]
                now = datetime.now().isoformat()
                
                if not local_hash and local_path.exists():
                    hasher = blake3.blake3()
                    async with aiofiles.open(local_path, 'rb') as f:
                        while chunk := await f.read(1024 * 1024):
                            hasher.update(chunk)
                    local_hash = hasher.hexdigest()
                    await db.execute(
                        "UPDATE file_index SET hash = ?, hash_computed_at = ? WHERE side = 'local' AND relpath = ?",
                        (local_hash, now, relpath)
                    )
                
                if not lake_hash and lake_path.exists():
                    hasher = blake3.blake3()
                    async with aiofiles.open(lake_path, 'rb') as f:
                        while chunk := await f.read(1024 * 1024):
                            hasher.update(chunk)
                    lake_hash = hasher.hexdigest()
                    await db.execute(
                        "UPDATE file_index SET hash = ?, hash_computed_at = ? WHERE side = 'lake' AND relpath = ?",
                        (lake_hash, now, relpath)
                    )
                
                verified += 1
                if local_hash == lake_hash:
                    matched += 1
                else:
                    mismatched += 1
                    print(f"Hash mismatch: {relpath}")
                    
            except Exception as e:
                print(f"Failed to verify {relpath}: {e}")
                continue
        
        await db.commit()
    
    duration = (datetime.now() - start).total_seconds() * 1000
    return VerifyResponse(
        verified=verified,
        matched=matched,
        mismatched=mismatched,
        duration_ms=round(duration, 2)
    )
