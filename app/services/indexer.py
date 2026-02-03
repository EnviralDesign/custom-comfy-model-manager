"""File indexing service - scans and caches file metadata."""

import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal

from app.config import get_settings
from app.database import get_db


class IndexerService:
    """Service for scanning and indexing files on Local and Lake."""
    
    def _get_root(self, side: Literal["local", "lake"]) -> Path:
        """Get the root path for a side."""
        settings = get_settings()
        if side == "local":
            return settings.local_models_root
        return settings.lake_models_root
    
    async def scan_side(self, side: Literal["local", "lake"]) -> int:
        """
        Scan a side and update the index.
        Returns the number of files indexed.
        """
        root = self._get_root(side)
        now = datetime.now(timezone.utc).isoformat()
        
        # Collect all files
        files_data = []
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                filepath = Path(dirpath) / filename
                try:
                    stat = filepath.stat()
                    relpath = str(filepath.relative_to(root))
                    # Normalize path separators to forward slashes
                    relpath = relpath.replace("\\", "/")
                    files_data.append({
                        "side": side,
                        "relpath": relpath,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "indexed_at": now,
                    })
                except (OSError, ValueError):
                    # Skip files we can't access
                    continue
        
        # Fetch existing hashes before deleting
        existing_hashes = {}
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT relpath, size, mtime_ns, hash, hash_computed_at FROM file_index WHERE side = ? AND hash IS NOT NULL",
                (side,)
            )
            rows = await cursor.fetchall()
            for row in rows:
                key = (row["relpath"], row["size"], row["mtime_ns"])
                existing_hashes[key] = (row["hash"], row["hash_computed_at"])
        
        # Prepare values for bulk insert
        insert_values = []
        for f in files_data:
            key = (f["relpath"], f["size"], f["mtime_ns"])
            if key in existing_hashes:
                # Reuse existing hash
                h, h_at = existing_hashes[key]
                insert_values.append((
                    f["side"], f["relpath"], f["size"], f["mtime_ns"], 
                    h, h_at, f["indexed_at"]
                ))
            else:
                # New file
                insert_values.append((
                    f["side"], f["relpath"], f["size"], f["mtime_ns"], 
                    None, None, f["indexed_at"]
                ))
        
        # Update DB in a single valid transaction
        async with get_db() as db:
            # Clear old entries for this side
            await db.execute("DELETE FROM file_index WHERE side = ?", (side,))
            
            # Batch insert
            await db.executemany(
                """
                INSERT INTO file_index (side, relpath, size, mtime_ns, hash, hash_computed_at, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                insert_values
            )
            await db.commit()
        
        return len(insert_values)
    
    async def get_files(
        self, 
        side: Literal["local", "lake"],
        folder: str = "",
        query: str = "",
    ) -> list[dict]:
        """
        Get files from the index.
        - folder: filter to files within this folder
        - query: fuzzy search on filename
        """
        async with get_db() as db:
            sql = "SELECT relpath, size, mtime_ns, hash FROM file_index WHERE side = ?"
            params: list = [side]
            
            if folder:
                # Normalize folder path
                folder = folder.replace("\\", "/").strip("/")
                sql += " AND relpath LIKE ?"
                params.append(f"{folder}/%")
            
            if query:
                # Simple LIKE search (could be improved with FTS)
                sql += " AND relpath LIKE ?"
                params.append(f"%{query}%")
            
            sql += " ORDER BY relpath"
            
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            
            return [
                {
                    "relpath": row["relpath"],
                    "size": row["size"],
                    "mtime_ns": row["mtime_ns"],
                    "hash": row["hash"],
                    "side": side,
                }
                for row in rows
            ]
    
    async def get_folders(
        self,
        side: Literal["local", "lake"],
        parent: str = "",
    ) -> list[str]:
        """Get immediate subfolders under a parent folder."""
        async with get_db() as db:
            if parent:
                parent = parent.replace("\\", "/").strip("/")
                prefix = f"{parent}/"
            else:
                prefix = ""
            
            # Get all relpaths and extract folder structure
            cursor = await db.execute(
                "SELECT DISTINCT relpath FROM file_index WHERE side = ?",
                (side,)
            )
            rows = await cursor.fetchall()
            
            folders = set()
            for row in rows:
                relpath: str = row["relpath"]
                if prefix and not relpath.startswith(prefix):
                    continue
                
                # Get the path after the prefix
                suffix = relpath[len(prefix):]
                # Get the first component (immediate subfolder)
                if "/" in suffix:
                    folder_name = suffix.split("/")[0]
                    folders.add(folder_name)
            
            return sorted(folders)
    
    async def get_stats(self, side: Literal["local", "lake"]) -> dict:
        """Get statistics for a side."""
        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT 
                    COUNT(*) as file_count,
                    COALESCE(SUM(size), 0) as total_bytes,
                    SUM(CASE WHEN hash IS NOT NULL THEN 1 ELSE 0 END) as hashed_count
                FROM file_index 
                WHERE side = ?
                """,
                (side,)
            )
            row = await cursor.fetchone()
            
            return {
                "file_count": row["file_count"],
                "total_bytes": row["total_bytes"],
                "hashed_count": row["hashed_count"],
            }
