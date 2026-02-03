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
            await broadcast("task_started", {"task_id": task_id, "task_type": task["task_type"]})
            
            if task["task_type"] == "copy":
                await self._execute_copy(task)
            elif task["task_type"] == "delete":
                await self._execute_delete(task)
            elif task["task_type"] == "verify":
                await self._execute_verify(task)
            elif task["task_type"] == "dedupe_scan":
                from app.services.dedupe import DedupeService
                import json
                
                # Parse config from dst_side
                try:
                    config = json.loads(task["dst_side"])
                    mode = config.get("mode", "full")
                    min_size = config.get("min_size", 0)
                except (json.JSONDecodeError, TypeError):
                    # Fallback for legacy or plain string
                    mode = task["dst_side"] if task["dst_side"] in ("full", "fast") else "full"
                    min_size = 0
                
                result = await DedupeService().execute_scan(task_id=task_id, side=task["src_side"], mode=mode, min_size_bytes=min_size)
                # Broadcast specific completion for dedupe to share scan_id
                await broadcast("task_complete", {
                    "task_id": task_id, 
                    "status": "completed", 
                    "result": result
                })
                # Skip the default broadcast below? No, duplicate broadcast is fine or we can return here.
                # But standard completion update in DB happens below.
                # Let's just store result in a field if we had one, but we don't.
                # We will rely on the "result" payload in the event.

            
            # Mark as completed
            async with get_db() as db:
                await db.execute(
                    "UPDATE queue SET status = 'completed', completed_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), task_id)
                )
                await db.commit()
            
            # Broadcast completion
            await broadcast("task_complete", {"task_id": task_id, "status": "completed"})
            
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
            
            await broadcast("task_complete", {"task_id": task_id, "status": "failed", "error": error_msg})
        
        finally:
            QueueWorker._current_task_id = None
    
    async def _execute_copy(self, task: dict):
        """Execute a copy task."""
        import blake3
        from datetime import datetime, timezone
        import time
        
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
        
        # Hash while copying
        hasher = blake3.blake3()
        
        # Copy with progress and compute hash
        chunk_size = 1024 * 1024  # 1MB chunks
        last_db_update_time = 0
        
        async with aiofiles.open(src_path, 'rb') as src_file:
            async with aiofiles.open(dst_path, 'wb') as dst_file:
                while True:
                    chunk = await src_file.read(chunk_size)
                    if not chunk:
                        break
                    await dst_file.write(chunk)
                    hasher.update(chunk)
                    bytes_copied += len(chunk)
                    
                    # Update progress in DB and broadcast
                    progress_pct = int((bytes_copied / file_size) * 100) if file_size > 0 else 100
                    
                    # Throttle DB updates to every 1 second or completion to avoid locking
                    current_time = time.time()
                    if current_time - last_db_update_time > 1.0 or bytes_copied == file_size:
                        async with get_db() as db:
                            await db.execute(
                                "UPDATE queue SET bytes_transferred = ? WHERE id = ?",
                                (bytes_copied, task_id)
                            )
                            await db.commit()
                        last_db_update_time = current_time
                    
                    # Broadcast progress (throttled to every 10% or completion)
                    if (progress_pct % 10 == 0 and progress_pct > 0) or bytes_copied == file_size:
                        await broadcast("queue_progress", {
                            "task_id": task_id,
                            "bytes_transferred": bytes_copied,
                            "total_bytes": file_size,
                            "progress_pct": progress_pct,
                        })
        
        # Compute final hash
        file_hash = hasher.hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        
        # Preserve file times (sync call is fine, very fast)
        import os
        src_stat = src_path.stat()
        os.utime(dst_path, (src_stat.st_atime, src_stat.st_mtime))
        dst_stat = dst_path.stat()
        
        # Update file_index with hash for both source and destination
        async with get_db() as db:
            # Update source file hash
            await db.execute(
                """
                UPDATE file_index SET hash = ?, hash_computed_at = ?
                WHERE side = ? AND relpath = ?
                """,
                (file_hash, now, task["src_side"], task["src_relpath"])
            )
            # Update destination file hash
            await db.execute(
                """
                INSERT OR REPLACE INTO file_index (side, relpath, size, mtime_ns, hash, hash_computed_at, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task["dst_side"], task["dst_relpath"], dst_stat.st_size, dst_stat.st_mtime_ns, file_hash, now, now)
            )
            await db.commit()
        
        print(f"Copied: {task['src_relpath']} → {task['dst_side']} (hash: {file_hash[:8]}...)")
    
    async def _execute_delete(self, task: dict):
        """Execute a delete task."""
        root = self._get_root(task["dst_side"])
        filepath = root / task["dst_relpath"].replace("/", "\\")
        
        if not filepath.exists():
            print(f"File already deleted: {filepath}")
            return
        
        await aiofiles.os.remove(filepath)
        print(f"Deleted: {task['dst_relpath']} from {task['dst_side']}")

    async def _execute_verify(self, task: dict):
        """Execute a verification task."""
        import blake3
        
        task_id = task["id"]
        relpath = task["src_relpath"]  # We reuse src_relpath for specific file
        folder = task["verify_folder"] # We added this column
        
        print(f"Verifying: {relpath if relpath else folder}")

        # Fetch candidate files
        candidate_files = []
        async with get_db() as db:
            if relpath:
                # Verify specific file
                start_sql = """
                    SELECT count(*) as count
                    FROM file_index l
                    JOIN file_index r ON l.relpath = r.relpath AND l.size = r.size
                    WHERE l.side = 'local' AND r.side = 'lake'
                    AND l.relpath = ?
                    AND (l.hash IS NULL OR r.hash IS NULL)
                """
                count_cursor = await db.execute(start_sql, (relpath,))
                
                sql = """
                    SELECT l.relpath, l.size, l.hash as local_hash, r.hash as lake_hash
                    FROM file_index l
                    JOIN file_index r ON l.relpath = r.relpath AND l.size = r.size
                    WHERE l.side = 'local' AND r.side = 'lake'
                    AND l.relpath = ?
                    AND (l.hash IS NULL OR r.hash IS NULL)
                """
                cursor = await db.execute(sql, (relpath,))
            else:
                # Verify folder or all
                if folder:
                    folder_prefix = folder.replace("\\", "/").strip("/")
                    start_sql = """
                        SELECT count(*) as count
                        FROM file_index l
                        JOIN file_index r ON l.relpath = r.relpath AND l.size = r.size
                        WHERE l.side = 'local' AND r.side = 'lake'
                        AND l.relpath LIKE ?
                        AND (l.hash IS NULL OR r.hash IS NULL)
                    """
                    count_cursor = await db.execute(start_sql, (f"{folder_prefix}/%",))
                    
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
                    # Scan root
                    start_sql = """
                        SELECT count(*) as count
                        FROM file_index l
                        JOIN file_index r ON l.relpath = r.relpath AND l.size = r.size
                        WHERE l.side = 'local' AND r.side = 'lake'
                        AND (l.hash IS NULL OR r.hash IS NULL)
                    """
                    count_cursor = await db.execute(start_sql)
                    
                    sql = """
                        SELECT l.relpath, l.size, l.hash as local_hash, r.hash as lake_hash
                        FROM file_index l
                        JOIN file_index r ON l.relpath = r.relpath AND l.size = r.size
                        WHERE l.side = 'local' AND r.side = 'lake'
                        AND (l.hash IS NULL OR r.hash IS NULL)
                    """
                    cursor = await db.execute(sql)
            
            # Update total size/count in DB for progress tracking
            row_count = await count_cursor.fetchone()
            total_files = row_count["count"]
            
            # Since we iterate files, let's use size_bytes as total_files for simplicity in UI
            # or we could use bytes if we query file sizes. Let's use file count for now.
            await db.execute(
                "UPDATE queue SET size_bytes = ? WHERE id = ?",
                (total_files, task_id)
            )
            await db.commit()
            
            candidate_files = await cursor.fetchall()
        
        # Process files
        verified_count = 0
        
        for i, row in enumerate(candidate_files):
            # Check for cancellation
            if not QueueWorker._running: 
                break
                
            file_relpath = row["relpath"]
            local_path = self.settings.local_models_root / file_relpath.replace("/", "\\")
            lake_path = self.settings.lake_models_root / file_relpath.replace("/", "\\")
            
            # Broadcast verify progress (reusing fields creatively or adding custom payload)
            # We can use 'queue_progress' but UI needs to interpret it.
            # verify_folder logic in UI expects 'verify_progress' event
            if folder:
                await broadcast("verify_progress", {
                    "folder": folder,
                    "current": i + 1,
                    "total": total_files,
                    "relpath": file_relpath
                })

            # Update queue progress
            async with get_db() as db:
                await db.execute(
                    "UPDATE queue SET bytes_transferred = ? WHERE id = ?",
                    (i + 1, task_id)
                )
                await db.commit()
                
            await broadcast("queue_progress", {
                "task_id": task_id,
                "bytes_transferred": i + 1,
                "total_bytes": total_files,
                "progress_pct": int(((i + 1) / total_files) * 100) if total_files > 0 else 100,
            })
            
            try:
                local_hash = row["local_hash"]
                lake_hash = row["lake_hash"]
                now = datetime.now(timezone.utc).isoformat()
                updates = []
                
                if not local_hash and local_path.exists():
                    hasher = blake3.blake3()
                    async with aiofiles.open(local_path, 'rb') as f:
                        while chunk := await f.read(1024 * 1024):
                            hasher.update(chunk)
                    local_hash = hasher.hexdigest()
                    updates.append(("local", local_hash))
                
                if not lake_hash and lake_path.exists():
                    hasher = blake3.blake3()
                    async with aiofiles.open(lake_path, 'rb') as f:
                        while chunk := await f.read(1024 * 1024):
                            hasher.update(chunk)
                    lake_hash = hasher.hexdigest()
                    updates.append(("lake", lake_hash))
                
                if updates:
                    async with get_db() as db:
                        for side, h in updates:
                            await db.execute(
                                "UPDATE file_index SET hash = ?, hash_computed_at = ? WHERE side = ? AND relpath = ?",
                                (h, now, side, file_relpath)
                            )
                        await db.commit()
                
                verified_count += 1
                    
            except Exception as e:
                print(f"Failed to verify {file_relpath}: {e}")
                continue
        
        print(f"Verification complete: {verified_count}/{total_files} files")


# Convenience functions
def get_worker() -> QueueWorker:
    return QueueWorker.get_instance()
