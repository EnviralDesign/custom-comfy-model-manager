"""Dedupe service for finding and removing duplicate files."""

import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel

from app.config import get_settings
from app.database import get_db
from app.services.hasher import HasherService


class DuplicateFile(BaseModel):
    id: int
    relpath: str
    size: int
    mtime_ns: int
    keep: bool


class DuplicateGroup(BaseModel):
    id: int
    hash: str
    files: list[DuplicateFile]


class DedupeService:
    def _get_root(self, side: str) -> Path:
        settings = get_settings()
        return settings.local_models_root if side == "local" else settings.lake_models_root
    
    async def scan(self, side: Literal["local", "lake"]) -> dict:
        """Scan for duplicates on one side."""
        scan_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        hasher = HasherService()
        
        # First, hash all files that don't have hashes
        await hasher.hash_all_pending(side)
        
        # Find duplicates by grouping by hash
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT hash, COUNT(*) as cnt FROM file_index WHERE side = ? AND hash IS NOT NULL GROUP BY hash HAVING cnt > 1",
                (side,)
            )
            dup_hashes = [row["hash"] for row in await cursor.fetchall()]
            
            total_files = 0
            reclaimable = 0
            
            for hash_val in dup_hashes:
                cursor = await db.execute(
                    "SELECT relpath, size, mtime_ns FROM file_index WHERE side = ? AND hash = ?",
                    (side, hash_val)
                )
                files = await cursor.fetchall()
                
                # Create group
                cursor = await db.execute(
                    "INSERT INTO dedupe_groups (side, hash, scan_id, created_at) VALUES (?, ?, ?, ?)",
                    (side, hash_val, scan_id, now)
                )
                group_id = cursor.lastrowid
                
                # Add files
                for i, f in enumerate(files):
                    await db.execute(
                        "INSERT INTO dedupe_files (group_id, relpath, size, mtime_ns, keep) VALUES (?, ?, ?, ?, ?)",
                        (group_id, f["relpath"], f["size"], f["mtime_ns"], 1 if i == 0 else 0)
                    )
                    total_files += 1
                    if i > 0:
                        reclaimable += f["size"]
            
            await db.commit()
        
        return {
            "scan_id": scan_id,
            "side": side,
            "total_files": total_files,
            "duplicate_groups": len(dup_hashes),
            "duplicate_files": total_files - len(dup_hashes),
            "reclaimable_bytes": reclaimable,
        }
    
    async def get_groups(self, scan_id: str) -> list[DuplicateGroup]:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT id, hash FROM dedupe_groups WHERE scan_id = ?", (scan_id,)
            )
            groups = []
            for row in await cursor.fetchall():
                cursor2 = await db.execute(
                    "SELECT id, relpath, size, mtime_ns, keep FROM dedupe_files WHERE group_id = ?",
                    (row["id"],)
                )
                files = [DuplicateFile(**dict(f)) for f in await cursor2.fetchall()]
                groups.append(DuplicateGroup(id=row["id"], hash=row["hash"], files=files))
            return groups
    
    async def execute(self, scan_id: str, selections: list) -> dict:
        """Execute dedupe - delete non-kept files. IGNORES allow-delete policy."""
        deleted = 0
        freed = 0
        errors = []
        
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT g.side, f.relpath, f.size FROM dedupe_groups g JOIN dedupe_files f ON g.id = f.group_id WHERE g.scan_id = ?",
                (scan_id,)
            )
            all_files = await cursor.fetchall()
        
        # Apply selections
        keep_set = {s.keep_relpath for s in selections}
        
        for f in all_files:
            if f["relpath"] not in keep_set:
                root = self._get_root(f["side"])
                filepath = root / f["relpath"].replace("/", "\\")
                try:
                    filepath.unlink()
                    deleted += 1
                    freed += f["size"]
                except Exception as e:
                    errors.append({"relpath": f["relpath"], "error": str(e)})
        
        return {"deleted": deleted, "freed_bytes": freed, "errors": errors}
    
    async def clear_scan(self, scan_id: str):
        async with get_db() as db:
            await db.execute("DELETE FROM dedupe_groups WHERE scan_id = ?", (scan_id,))
            await db.commit()
