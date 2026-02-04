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
    task_type: Literal["copy", "move", "delete", "verify", "dedupe_scan", "hash_file"]
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

    def _resolve_move_paths(self, side: str, src_relpath: str, dst_relpath: str) -> tuple[Path, Path]:
        root = self._get_root(side)
        src_path = root / src_relpath.replace("/", "\\")
        dst_path = root / dst_relpath.replace("/", "\\")
        return src_path, dst_path

    def _get_move_status(self, side: str, src_relpath: str, dst_relpath: str) -> dict:
        if src_relpath == dst_relpath:
            return {
                "side": side,
                "ok": False,
                "reason": "same_path",
                "message": "source and destination are the same",
                "src_exists": None,
                "dst_exists": None,
            }
        src_path, dst_path = self._resolve_move_paths(side, src_relpath, dst_relpath)
        src_exists = src_path.exists()
        dst_exists = dst_path.exists()
        if not src_exists:
            return {
                "side": side,
                "ok": False,
                "reason": "missing_source",
                "message": f"{side} source not found",
                "src_exists": src_exists,
                "dst_exists": dst_exists,
            }
        if dst_exists:
            return {
                "side": side,
                "ok": False,
                "reason": "destination_exists",
                "message": f"{side} destination already exists",
                "src_exists": src_exists,
                "dst_exists": dst_exists,
            }
        return {
            "side": side,
            "ok": True,
            "reason": None,
            "message": "ok",
            "src_exists": src_exists,
            "dst_exists": dst_exists,
        }

    def _validate_move(self, side: str, src_relpath: str, dst_relpath: str) -> tuple[bool, str]:
        if src_relpath == dst_relpath:
            return False, "Source and destination are the same"
        src_path, dst_path = self._resolve_move_paths(side, src_relpath, dst_relpath)
        if not src_path.exists():
            return False, f"{side} source not found"
        if dst_path.exists():
            return False, f"{side} destination already exists"
        return True, ""

    def preflight_move(self, sides: list[str], src_relpath: str, dst_relpath: str) -> dict:
        unique_sides: list[str] = []
        for side in sides:
            if side not in unique_sides:
                unique_sides.append(side)
        statuses = [self._get_move_status(side, src_relpath, dst_relpath) for side in unique_sides]
        return {"sides": statuses}
    
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

    async def enqueue_move(self, side: str, src_relpath: str, dst_relpath: str) -> int:
        task_ids = await self.enqueue_move_batch([side], src_relpath, dst_relpath)
        return task_ids[0] if task_ids else 0

    async def enqueue_move_batch(self, sides: list[str], src_relpath: str, dst_relpath: str) -> list[int]:
        if not sides:
            raise ValueError("No sides selected for move")

        if src_relpath == dst_relpath:
            raise ValueError("Move blocked: source and destination are the same")

        unique_sides: list[str] = []
        for side in sides:
            if side not in unique_sides:
                unique_sides.append(side)

        errors: list[str] = []
        sizes: dict[str, int] = {}
        for side in unique_sides:
            ok, message = self._validate_move(side, src_relpath, dst_relpath)
            if not ok:
                errors.append(message)
                continue
            src_path, _ = self._resolve_move_paths(side, src_relpath, dst_relpath)
            sizes[side] = src_path.stat().st_size

        if errors:
            raise ValueError("Move blocked: " + "; ".join(errors))

        now = datetime.now(timezone.utc).isoformat()
        task_ids: list[int] = []
        async with get_db() as db:
            for side in unique_sides:
                cursor = await db.execute(
                    "INSERT INTO queue (task_type, src_side, src_relpath, dst_side, dst_relpath, size_bytes, created_at) VALUES ('move', ?, ?, ?, ?, ?, ?)",
                    (side, src_relpath, side, dst_relpath, sizes.get(side, 0), now)
                )
                task_ids.append(cursor.lastrowid or 0)
            await db.commit()
        return task_ids
    
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

    async def cancel_all_tasks(self) -> int:
        """Cancel all pending and running tasks."""
        async with get_db() as db:
            cursor = await db.execute(
                "UPDATE queue SET status = 'cancelled', completed_at = ? WHERE status IN ('pending', 'running')",
                (datetime.now(timezone.utc).isoformat(),)
            )
            await db.commit()
            return cursor.rowcount
    
    async def pause(self): QueueService._paused = True
    async def resume(self): QueueService._paused = False
