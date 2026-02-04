"""Diff computation between Local and Lake."""

from pydantic import BaseModel
from typing import Literal

from app.database import get_db


class DiffEntry(BaseModel):
    """A single diff entry comparing Local and Lake."""
    
    relpath: str
    status: Literal["only_local", "only_lake", "same", "probable_same", "conflict"]
    local_size: int | None = None
    local_mtime_ns: int | None = None
    local_hash: str | None = None
    lake_size: int | None = None
    lake_mtime_ns: int | None = None
    lake_hash: str | None = None


async def compute_diff(
    folder: str = "",
    query: str = "",
) -> list[DiffEntry]:
    """
    Compute diff between Local and Lake.
    
    Status logic:
    - only_local: file exists only on Local
    - only_lake: file exists only on Lake  
    - same: both exist and hashes match
    - probable_same: both exist, hashes pending, but size+mtime match
    - conflict: both exist but hashes differ, or sizes differ
    """
    async with get_db() as db:
        # Build query conditions
        conditions = []
        params: list = []
        
        if folder:
            folder = folder.replace("\\", "/").strip("/")
            conditions.append("relpath LIKE ?")
            params.append(f"{folder}/%")
        
        if query:
            conditions.append("relpath LIKE ?")
            params.append(f"%{query}%")
        
        where_clause = " AND " + " AND ".join(conditions) if conditions else ""
        
        # Get all local files
        cursor = await db.execute(
            f"SELECT relpath, size, mtime_ns, hash FROM file_index WHERE side = 'local'{where_clause}",
            params
        )
        local_files = {row["relpath"]: dict(row) for row in await cursor.fetchall()}
        
        # Get all lake files
        cursor = await db.execute(
            f"SELECT relpath, size, mtime_ns, hash FROM file_index WHERE side = 'lake'{where_clause}",
            params
        )
        lake_files = {row["relpath"]: dict(row) for row in await cursor.fetchall()}
    
    # Compute diff
    all_relpaths = set(local_files.keys()) | set(lake_files.keys())
    diff_entries: list[DiffEntry] = []
    
    for relpath in sorted(all_relpaths):
        local = local_files.get(relpath)
        lake = lake_files.get(relpath)
        
        if local and not lake:
            diff_entries.append(DiffEntry(
                relpath=relpath,
                status="only_local",
                local_size=local["size"],
                local_mtime_ns=local["mtime_ns"],
                local_hash=local["hash"],
            ))
        
        elif lake and not local:
            diff_entries.append(DiffEntry(
                relpath=relpath,
                status="only_lake",
                lake_size=lake["size"],
                lake_mtime_ns=lake["mtime_ns"],
                lake_hash=lake["hash"],
            ))
        
        else:
            # Both exist - compare
            assert local and lake
            
            local_hash = local["hash"]
            lake_hash = lake["hash"]
            
            if local_hash and lake_hash:
                # Both hashed - compare
                if local_hash == lake_hash:
                    status = "same"
                else:
                    status = "conflict"
            else:
                # At least one hash pending - check size+mtime
                if local["size"] != lake["size"]:
                    status = "conflict"
                elif local["size"] == lake["size"] and local["mtime_ns"] == lake["mtime_ns"]:
                    status = "probable_same"
                else:
                    # Different size/mtime and no hashes - could be conflict
                    # Mark as probable_same for now, full hash needed
                    status = "probable_same"
            
            diff_entries.append(DiffEntry(
                relpath=relpath,
                status=status,
                local_size=local["size"],
                local_mtime_ns=local["mtime_ns"],
                local_hash=local_hash,
                lake_size=lake["size"],
                lake_mtime_ns=lake["mtime_ns"],
                lake_hash=lake_hash,
            ))
    
    return diff_entries
