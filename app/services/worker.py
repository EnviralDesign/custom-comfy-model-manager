"""Background queue worker for processing file transfers."""

import asyncio
import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable

import aiofiles
import aiofiles.os

from app.config import get_settings
from app.database import get_db
from app.websocket import broadcast


class QueueWorker:
    """Background worker that processes queue tasks."""
    
    _instance = None
    _running = False
    _paused = False
    _current_task_id = None
    
    def __init__(self):
        self.settings = get_settings()
    
    @classmethod
    def get_instance(cls) -> "QueueWorker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def _get_root(self, side: str) -> Path:
        if side == "local":
            return self.settings.local_models_root
        return self.settings.lake_models_root
    
    async def start(self):
        """Start the worker loop."""
        if QueueWorker._running:
            return
        QueueWorker._running = True
        print("✓ Queue worker started")
        asyncio.create_task(self._worker_loop())
    
    async def stop(self):
        """Stop the worker loop."""
        QueueWorker._running = False
        print("Queue worker stopped")
    
    @classmethod
    def pause(cls):
        cls._paused = True
        print("Queue worker paused")
    
    @classmethod
    def resume(cls):
        cls._paused = False
        print("Queue worker resumed")
    
    @classmethod
    def is_paused(cls) -> bool:
        return cls._paused
    
    async def _worker_loop(self):
        """Main worker loop - continuously process queue tasks."""
        while QueueWorker._running:
            try:
                if not QueueWorker._paused:
                    task = await self._get_next_task()
                    if task:
                        await self._process_task(task)
                    else:
                        # No tasks, wait a bit before checking again
                        await asyncio.sleep(1)
                else:
                    # Paused, check less frequently
                    await asyncio.sleep(2)
            except Exception as e:
                print(f"Queue worker error: {e}")
                await asyncio.sleep(5)
    
    async def _get_next_task(self) -> dict | None:
        """Get the next pending task from the queue."""
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
        return None
    
    async def _process_task(self, task: dict):
        """Process a single queue task."""
        task_id = task["id"]
        QueueWorker._current_task_id = task_id
        
        try:
            # Mark as running
            async with get_db() as db:
                await db.execute(
                    "UPDATE queue SET status = 'running', started_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), task_id)
                )
                await db.commit()
            
            # Broadcast status
            await broadcast({
                "type": "task_started",
                "data": {"task_id": task_id, "task_type": task["task_type"]}
            })
            
            if task["task_type"] == "copy":
                await self._execute_copy(task)
            elif task["task_type"] == "delete":
                await self._execute_delete(task)
            
            # Mark as completed
            async with get_db() as db:
                await db.execute(
                    "UPDATE queue SET status = 'completed', completed_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), task_id)
                )
                await db.commit()
            
            # Broadcast completion
            await broadcast({
                "type": "task_complete",
                "data": {"task_id": task_id, "status": "completed"}
            })
            
        except asyncio.CancelledError:
            # Task was cancelled
            async with get_db() as db:
                await db.execute(
                    "UPDATE queue SET status = 'cancelled', completed_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), task_id)
                )
                await db.commit()
            
        except Exception as e:
            # Task failed
            error_msg = str(e)
            print(f"Task {task_id} failed: {error_msg}")
            
            async with get_db() as db:
                await db.execute(
                    """UPDATE queue SET 
                        status = 'failed', 
                        error_message = ?, 
                        completed_at = ?,
                        retry_count = retry_count + 1
                    WHERE id = ?""",
                    (error_msg, datetime.now(timezone.utc).isoformat(), task_id)
                )
                await db.commit()
            
            await broadcast({
                "type": "task_complete",
                "data": {"task_id": task_id, "status": "failed", "error": error_msg}
            })
        
        finally:
            QueueWorker._current_task_id = None
    
    async def _execute_copy(self, task: dict):
        """Execute a copy task."""
        src_root = self._get_root(task["src_side"])
        dst_root = self._get_root(task["dst_side"])
        
        src_path = src_root / task["src_relpath"].replace("/", "\\")
        dst_path = dst_root / task["dst_relpath"].replace("/", "\\")
        
        if not src_path.exists():
            raise FileNotFoundError(f"Source file not found: {src_path}")
        
        # Create destination directory if needed
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Get file size for progress
        file_size = src_path.stat().st_size
        bytes_copied = 0
        task_id = task["id"]
        
        # Copy with progress
        chunk_size = 1024 * 1024  # 1MB chunks
        
        async with aiofiles.open(src_path, 'rb') as src_file:
            async with aiofiles.open(dst_path, 'wb') as dst_file:
                while True:
                    chunk = await src_file.read(chunk_size)
                    if not chunk:
                        break
                    await dst_file.write(chunk)
                    bytes_copied += len(chunk)
                    
                    # Update progress in DB and broadcast
                    progress_pct = int((bytes_copied / file_size) * 100) if file_size > 0 else 100
                    
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE queue SET bytes_transferred = ? WHERE id = ?",
                            (bytes_copied, task_id)
                        )
                        await db.commit()
                    
                    # Broadcast progress (throttled to every 10%)
                    if progress_pct % 10 == 0 or bytes_copied == file_size:
                        await broadcast({
                            "type": "queue_progress",
                            "data": {
                                "task_id": task_id,
                                "bytes_transferred": bytes_copied,
                                "total_bytes": file_size,
                                "progress_pct": progress_pct,
                            }
                        })
        
        # Preserve file times
        src_stat = src_path.stat()
        await aiofiles.os.utime(dst_path, (src_stat.st_atime, src_stat.st_mtime))
        
        print(f"Copied: {task['src_relpath']} → {task['dst_side']}")
    
    async def _execute_delete(self, task: dict):
        """Execute a delete task."""
        root = self._get_root(task["dst_side"])
        filepath = root / task["dst_relpath"].replace("/", "\\")
        
        if not filepath.exists():
            print(f"File already deleted: {filepath}")
            return
        
        await aiofiles.os.remove(filepath)
        print(f"Deleted: {task['dst_relpath']} from {task['dst_side']}")


# Convenience functions
def get_worker() -> QueueWorker:
    return QueueWorker.get_instance()
