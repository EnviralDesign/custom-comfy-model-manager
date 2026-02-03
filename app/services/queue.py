"""Queue service for managing transfer and delete operations."""

import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel

from app.config import get_settings
from app.database import get_db


class QueueTask(BaseModel):
    id: int
    task_type: Literal["copy", "delete"]
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    src_side: str | None
    src_relpath: str | None
    dst_side: str | None
    dst_relpath: str | None
    size_bytes: int | None
    bytes_transferred: int
    error_message: str | None
    retry_count: int
    created_at: str
    started_at: str | None
    completed_at: str | None


class QueueService:
    _paused: bool = False
    
    def _get_root(self, side: str) -> Path:
        settings = get_settings()
        return settings.local_models_root if side == "local" else settings.lake_models_root
    
    async def get_all_tasks(self) -> list[QueueTask]:
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM queue ORDER BY created_at DESC")
            return [QueueTask(**dict(row)) for row in await cursor.fetchall()]
    
    async def get_active_task(self) -> QueueTask | None:
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM queue WHERE status = 'running' LIMIT 1")
            row = await cursor.fetchone()
            return QueueTask(**dict(row)) if row else None
    
    async def enqueue_copy(self, src_side: str, src_relpath: str, dst_side: str, dst_relpath: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        src_path = self._get_root(src_side) / src_relpath.replace("/", "\\")
        size = src_path.stat().st_size if src_path.exists() else 0
        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO queue (task_type, src_side, src_relpath, dst_side, dst_relpath, size_bytes, created_at) VALUES ('copy', ?, ?, ?, ?, ?, ?)",
                (src_side, src_relpath, dst_side, dst_relpath, size, now)
            )
            await db.commit()
            return cursor.lastrowid or 0
    
    async def enqueue_delete(self, side: str, relpath: str, respect_policy: bool = True) -> int:
        if respect_policy:
            settings = get_settings()
            if side == "local" and not settings.local_allow_delete:
                raise ValueError("Delete not allowed on Local")
            if side == "lake" and not settings.lake_allow_delete:
                raise ValueError("Delete not allowed on Lake")
        now = datetime.now(timezone.utc).isoformat()
        filepath = self._get_root(side) / relpath.replace("/", "\\")
        size = filepath.stat().st_size if filepath.exists() else 0
        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO queue (task_type, dst_side, dst_relpath, size_bytes, created_at) VALUES ('delete', ?, ?, ?, ?)",
                (side, relpath, size, now)
            )
            await db.commit()
            return cursor.lastrowid or 0
    
    async def cancel_task(self, task_id: int) -> bool:
        async with get_db() as db:
            cursor = await db.execute(
                "UPDATE queue SET status = 'cancelled', completed_at = ? WHERE id = ? AND status IN ('pending', 'running')",
                (datetime.now(timezone.utc).isoformat(), task_id)
            )
            await db.commit()
            return cursor.rowcount > 0
    
    async def remove_task(self, task_id: int) -> bool:
        async with get_db() as db:
            cursor = await db.execute("DELETE FROM queue WHERE id = ? AND status = 'pending'", (task_id,))
            await db.commit()
            return cursor.rowcount > 0
    
    async def pause(self): QueueService._paused = True
    async def resume(self): QueueService._paused = False
