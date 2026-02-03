"""BLAKE3 hashing service with caching."""

import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal, Callable
from concurrent.futures import ThreadPoolExecutor

import blake3

from app.config import get_settings
from app.database import get_db

# Thread pool for CPU-bound hashing
_hash_executor: ThreadPoolExecutor | None = None


def get_hash_executor() -> ThreadPoolExecutor:
    """Get or create the hash thread pool."""
    global _hash_executor
    if _hash_executor is None:
        settings = get_settings()
        _hash_executor = ThreadPoolExecutor(
            max_workers=settings.hash_workers,
            thread_name_prefix="hasher"
        )
    return _hash_executor


def compute_hash_sync(filepath: Path, progress_callback: Callable[[int], None] | None = None) -> str:
    """
    Compute BLAKE3 hash of a file synchronously.
    
    Args:
        filepath: Path to the file
        progress_callback: Optional callback(bytes_read) for progress
    
    Returns:
        Hex-encoded hash string
    """
    hasher = blake3.blake3()
    chunk_size = 1024 * 1024  # 1MB chunks
    bytes_read = 0
    
    with open(filepath, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
            bytes_read += len(chunk)
            if progress_callback:
                progress_callback(bytes_read)
    
    return hasher.hexdigest()


class HasherService:
    """Service for computing and caching BLAKE3 hashes."""
    
    def _get_root(self, side: Literal["local", "lake"]) -> Path:
        """Get the root path for a side."""
        settings = get_settings()
        if side == "local":
            return settings.local_models_root
        return settings.lake_models_root
    
    async def get_hash(
        self,
        side: Literal["local", "lake"],
        relpath: str,
        force: bool = False,
    ) -> str | None:
        """
        Get the hash for a file, computing if necessary.
        
        Uses cache if size+mtime unchanged.
        Returns None if file doesn't exist.
        """
        root = self._get_root(side)
        filepath = root / relpath.replace("/", "\\")
        
        if not filepath.exists():
            return None
        
        stat = filepath.stat()
        
        if not force:
            # Check cache
            async with get_db() as db:
                cursor = await db.execute(
                    """
                    SELECT hash FROM file_index 
                    WHERE side = ? AND relpath = ? AND size = ? AND mtime_ns = ? AND hash IS NOT NULL
                    """,
                    (side, relpath.replace("\\", "/"), stat.st_size, stat.st_mtime_ns)
                )
                row = await cursor.fetchone()
                if row:
                    return row["hash"]
        
        # Compute hash in thread pool
        loop = asyncio.get_event_loop()
        hash_value = await loop.run_in_executor(
            get_hash_executor(),
            compute_hash_sync,
            filepath,
            None
        )
        
        # Update cache
        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as db:
            await db.execute(
                """
                UPDATE file_index 
                SET hash = ?, hash_computed_at = ?
                WHERE side = ? AND relpath = ?
                """,
                (hash_value, now, side, relpath.replace("\\", "/"))
            )
            await db.commit()
        
        return hash_value
    
    async def hash_all_pending(
        self,
        side: Literal["local", "lake"],
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> int:
        """
        Hash all files on a side that don't have a hash yet.
        
        Args:
            side: Which side to hash
            progress_callback: Optional callback(current, total, relpath)
        
        Returns:
            Number of files hashed
        """
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT relpath FROM file_index WHERE side = ? AND hash IS NULL",
                (side,)
            )
            pending = [row["relpath"] for row in await cursor.fetchall()]
        
        total = len(pending)
        for i, relpath in enumerate(pending):
            await self.get_hash(side, relpath)
            if progress_callback:
                progress_callback(i + 1, total, relpath)
        
        return total
