"""Background queue worker for processing file transfers."""

import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
import aiofiles.os

from app.config import get_settings
from app.database import get_db
from app.services.queue_paths import resolve_queue_path
from app.services.queue_scheduler import (
    LANE_CLEANUP,
    LANE_INTEGRITY,
    LANE_TRANSFER,
    select_runnable_tasks,
    task_lane,
    task_resources,
)
from app.websocket import broadcast


class QueueWorker:
    """Concurrent, path-safe background scheduler for file operations."""

    _instance = None
    _running = False
    _paused = False

    def __init__(self):
        self.settings = get_settings()
        self._scheduler_task: asyncio.Task | None = None
        self._active: dict[int, dict] = {}
        self._integrity_idle_since: float | None = None

    @classmethod
    def get_instance(cls) -> "QueueWorker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _get_root(self, side: str) -> Path:
        if side == "local":
            return self.settings.local_models_root
        if side == "lake":
            return self.settings.lake_models_root
        raise ValueError(f"Unknown queue side: {side}")

    def _resolve_path(self, side: str, relpath: str) -> Path:
        path, _ = resolve_queue_path(self._get_root(side), relpath)
        return path

    async def start(self):
        """Start the scheduler loop."""
        if QueueWorker._running:
            return
        await self._recover_interrupted_tasks()
        QueueWorker._running = True
        self._scheduler_task = asyncio.create_task(self._worker_loop(), name="queue-scheduler")
        print(
            "✓ Queue scheduler started "
            f"(transfers={max(1, self.settings.queue_concurrency)}, "
            f"cleanup={max(1, self.settings.queue_cleanup_concurrency)}, integrity=idle-only)"
        )

    async def _recover_interrupted_tasks(self) -> None:
        """Requeue work left running by an unclean single-process shutdown."""
        async with get_db() as db:
            await db.execute(
                """
                UPDATE queue
                SET status = 'pending', started_at = NULL, completed_at = NULL,
                    error_message = NULL
                WHERE status = 'running'
                """
            )
            await db.commit()

    async def stop(self):
        """Stop scheduling and cooperatively cancel active work."""
        QueueWorker._running = False
        self.abort_all_tasks()
        if self._scheduler_task:
            await self._scheduler_task
            self._scheduler_task = None
        if self._active:
            await asyncio.gather(
                *(execution["task"] for execution in list(self._active.values())),
                return_exceptions=True,
            )
        print("Queue scheduler stopped")

    @classmethod
    def pause(cls):
        cls._paused = True
        print("Queue scheduler paused")

    @classmethod
    def resume(cls):
        cls._paused = False
        print("Queue scheduler resumed")

    @classmethod
    def is_paused(cls) -> bool:
        return cls._paused

    def request_task_cancellation(self, task_id: int) -> str | None:
        """Request cancellation without racing an irreversible filesystem commit."""
        execution = self._active.get(task_id)
        if not execution:
            return None
        if execution.get("commit_started"):
            return "too_late"
        execution["cancel_event"].set()
        return "requested"

    def cancel_task(self, task_id: int) -> bool:
        """Compatibility wrapper for callers that only need accepted/rejected."""
        return self.request_task_cancellation(task_id) == "requested"

    def abort_all_tasks(self) -> tuple[int, int]:
        """Signal cancellable work and report irreversible work separately."""
        requested = 0
        too_late = 0
        for execution in self._active.values():
            if execution.get("commit_started"):
                too_late += 1
                continue
            execution["cancel_event"].set()
            requested += 1
        return requested, too_late

    @classmethod
    def abort_current_task(cls):
        """Backward-compatible alias: concurrency means abort every active task."""
        if cls._instance:
            cls._instance.abort_all_tasks()

    async def _worker_loop(self):
        """Continuously claim the maximal safe set of runnable tasks."""
        while QueueWorker._running:
            try:
                if QueueWorker._paused:
                    await asyncio.sleep(max(0.1, self.settings.queue_scheduler_poll_seconds))
                    continue

                pending = await self._get_pending_tasks()
                roots = {
                    "local": self.settings.local_models_root,
                    "lake": self.settings.lake_models_root,
                }
                selected = select_runnable_tasks(
                    pending=pending,
                    active=list(self._active.values()),
                    roots=roots,
                    lane_limits={
                        LANE_TRANSFER: max(1, self.settings.queue_concurrency),
                        LANE_CLEANUP: max(1, self.settings.queue_cleanup_concurrency),
                    },
                )
                selected = await self._apply_integrity_idle_policy(pending, selected)

                for task in selected:
                    if await self._claim_task(task["id"]):
                        self._launch_task(task, roots)

                await asyncio.sleep(max(0.05, self.settings.queue_scheduler_poll_seconds))
            except Exception as exc:
                print(f"Queue scheduler error: {exc}")
                await asyncio.sleep(1)

    async def _apply_integrity_idle_policy(
        self,
        pending: list[dict],
        selected: list[dict],
    ) -> list[dict]:
        """Require a quiet window before starting globally-exclusive disk work."""
        normal_pending = any(task_lane(task) != LANE_INTEGRITY for task in pending)
        normal_active = any(
            execution["lane"] != LANE_INTEGRITY for execution in self._active.values()
        )
        integrity_active = any(
            execution["lane"] == LANE_INTEGRITY for execution in self._active.values()
        )
        downloads_busy = await self._has_active_downloads()

        if integrity_active:
            if normal_pending or downloads_busy:
                self._integrity_idle_since = None
                for execution in self._active.values():
                    if execution["lane"] == LANE_INTEGRITY:
                        execution["requeue_on_cancel"] = True
                        execution["cancel_event"].set()
            return []

        if normal_pending or normal_active:
            self._integrity_idle_since = None
            return selected

        if downloads_busy:
            self._integrity_idle_since = None
            return []

        now = time.monotonic()
        if self._integrity_idle_since is None:
            self._integrity_idle_since = now

        if selected and task_lane(selected[0]) == LANE_INTEGRITY:
            idle_for = now - self._integrity_idle_since
            if idle_for < max(0.0, self.settings.queue_integrity_idle_seconds):
                return []
        return selected

    async def _has_active_downloads(self) -> bool:
        """Keep integrity I/O idle while model downloads are queued or running."""
        try:
            async with get_db() as db:
                cursor = await db.execute(
                    "SELECT 1 FROM download_jobs WHERE status IN ('queued', 'running') LIMIT 1"
                )
                return await cursor.fetchone() is not None
        except sqlite3.OperationalError:
            # Small isolated test databases may only define the queue table.
            return False

    async def _get_pending_tasks(self) -> list[dict]:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM queue WHERE status = 'pending' ORDER BY created_at ASC, id ASC"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _claim_task(self, task_id: int) -> bool:
        """Atomically claim work, including the download/integrity exclusion."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT task_type FROM queue WHERE id = ? AND status = 'pending'",
                (task_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return False

            if row[0] in {"hash_file", "verify", "dedupe_scan"}:
                cursor = await db.execute(
                    """
                    SELECT 1 FROM download_jobs
                    WHERE status IN ('queued', 'running')
                    LIMIT 1
                    """
                )
                if await cursor.fetchone() is not None:
                    await db.rollback()
                    return False

            cursor = await db.execute(
                """
                UPDATE queue
                SET status = 'running', started_at = ?, completed_at = NULL,
                    error_message = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (datetime.now(timezone.utc).isoformat(), task_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    def _launch_task(self, task: dict, roots: dict[str, Path]) -> None:
        task_id = int(task["id"])
        execution = {
            "lane": task_lane(task),
            "resources": task_resources(task, roots),
            "cancel_event": asyncio.Event(),
            "requeue_on_cancel": False,
            "commit_started": False,
        }
        execution["task"] = asyncio.create_task(
            self._run_claimed_task(task),
            name=f"queue-{task['task_type']}-{task_id}",
        )
        self._active[task_id] = execution

    def _is_cancelled(self, task_id: int) -> bool:
        execution = self._active.get(task_id)
        return (
            not QueueWorker._running
            or execution is None
            or execution["cancel_event"].is_set()
        )

    def _raise_if_cancelled(self, task_id: int) -> None:
        if self._is_cancelled(task_id):
            raise asyncio.CancelledError(f"Task {task_id} cancelled")

    def _begin_irreversible_commit(self, task_id: int) -> None:
        self._raise_if_cancelled(task_id)
        execution = self._active.get(task_id)
        if execution is None:
            raise asyncio.CancelledError(f"Task {task_id} cancelled")
        execution["commit_started"] = True

    async def _set_operation_phase(self, task_id: int, phase: str) -> None:
        async with get_db() as db:
            await db.execute(
                "UPDATE queue SET operation_phase = ? WHERE id = ? AND status = 'running'",
                (phase, task_id),
            )
            await db.commit()

    async def _run_claimed_task(self, task: dict):
        """Execute a task that has already been atomically claimed."""
        task_id = int(task["id"])
        try:
            await broadcast("task_started", {"task_id": task_id, "task_type": task["task_type"]})
            await self._execute_task_body(task)

            execution = self._active.get(task_id)
            if (
                execution
                and not execution.get("commit_started")
                and execution["cancel_event"].is_set()
            ):
                raise asyncio.CancelledError(f"Task {task_id} cancelled before completion")
            if execution:
                # No await between the last cancellation check and closing the
                # cancellation window: API requests now receive "too_late".
                execution["commit_started"] = True

            async with get_db() as db:
                cursor = await db.execute(
                    """
                    UPDATE queue SET status = 'completed', completed_at = ?,
                        operation_phase = 'completed'
                    WHERE id = ? AND status = 'running'
                    """,
                    (datetime.now(timezone.utc).isoformat(), task_id),
                )
                await db.commit()
            if cursor.rowcount != 1:
                return

            await broadcast("task_complete", {
                "task_id": task_id,
                "status": "completed",
                "task_type": task["task_type"],
                "src_relpath": task.get("src_relpath"),
                "dst_relpath": task.get("dst_relpath"),
                "src_side": task.get("src_side"),
                "dst_side": task.get("dst_side"),
            })

        except asyncio.CancelledError:
            execution = self._active.get(task_id)
            should_requeue = bool(
                QueueWorker._running
                and execution
                and execution.get("requeue_on_cancel")
            )
            if should_requeue:
                async with get_db() as db:
                    cursor = await db.execute(
                        """
                        UPDATE queue
                        SET status = 'pending', started_at = NULL, completed_at = NULL,
                            bytes_transferred = 0, error_message = NULL
                        WHERE id = ? AND status = 'running'
                        """,
                        (task_id,),
                    )
                    await db.commit()
                if cursor.rowcount == 1:
                    await broadcast("task_deferred", {
                        "task_id": task_id,
                        "status": "pending",
                        "task_type": task["task_type"],
                        "reason": "higher_priority_io",
                    })
                    return

            async with get_db() as db:
                await db.execute(
                    """
                    UPDATE queue SET status = 'cancelled', completed_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (datetime.now(timezone.utc).isoformat(), task_id),
                )
                await db.commit()
            await broadcast("task_complete", {
                "task_id": task_id,
                "status": "cancelled",
                "task_type": task["task_type"],
            })

        except Exception as exc:
            error_msg = str(exc)
            print(f"Task {task_id} failed: {error_msg}")
            async with get_db() as db:
                cursor = await db.execute(
                    """UPDATE queue SET
                        status = 'failed', error_message = ?, completed_at = ?,
                        retry_count = retry_count + 1
                    WHERE id = ? AND status = 'running'""",
                    (error_msg, datetime.now(timezone.utc).isoformat(), task_id),
                )
                await db.commit()
            if cursor.rowcount == 1:
                await broadcast("task_complete", {
                    "task_id": task_id,
                    "status": "failed",
                    "task_type": task["task_type"],
                    "error": error_msg,
                })
        finally:
            self._active.pop(task_id, None)

    async def _execute_task_body(self, task: dict) -> None:
        task_id = int(task["id"])
        self._raise_if_cancelled(task_id)

        if task["task_type"] == "copy":
            await self._execute_copy(task)
        elif task["task_type"] == "move":
            await self._execute_move(task)
        elif task["task_type"] == "delete":
            await self._execute_delete(task)
        elif task["task_type"] == "verify":
            await self._execute_verify(task)
        elif task["task_type"] == "hash_file":
            await self._execute_hash_file(task)
        elif task["task_type"] == "dedupe_scan":
            import json

            from app.services.dedupe import DedupeService

            try:
                config = json.loads(task["dst_side"])
                mode = config.get("mode", "full")
                min_size = config.get("min_size", 0)
            except (json.JSONDecodeError, TypeError):
                mode = task["dst_side"] if task["dst_side"] in ("full", "fast") else "full"
                min_size = 0

            await DedupeService().execute_scan(
                task_id=task_id,
                side=task["src_side"],
                mode=mode,
                min_size_bytes=min_size,
                cancel_check=lambda: self._is_cancelled(task_id),
            )
        else:
            raise ValueError(f"Unsupported queue task type: {task['task_type']}")
    
    async def _execute_copy(self, task: dict):
        """Copy through a same-directory staging file, then atomically commit."""
        import blake3
        
        src_path = self._resolve_path(task["src_side"], task["src_relpath"])
        dst_path = self._resolve_path(task["dst_side"], task["dst_relpath"])
        
        if not src_path.exists() or not src_path.is_file():
            raise FileNotFoundError(f"Source file not found: {src_path}")
        
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        task_id = int(task["id"])
        staging_path = dst_path.with_name(f".{dst_path.name}.cmm-{task_id}.part")
        if staging_path.exists():
            staging_path.unlink()

        file_size = src_path.stat().st_size
        bytes_copied = 0
        hasher = blake3.blake3()
        chunk_size = 1024 * 1024  # 1MB chunks
        last_db_update_time = 0

        try:
            async with aiofiles.open(src_path, "rb") as src_file:
                async with aiofiles.open(staging_path, "wb") as dst_file:
                    while True:
                        self._raise_if_cancelled(task_id)
                        chunk = await src_file.read(chunk_size)
                        if not chunk:
                            break
                        await dst_file.write(chunk)
                        hasher.update(chunk)
                        bytes_copied += len(chunk)

                        progress_pct = int((bytes_copied / file_size) * 100) if file_size > 0 else 100
                        current_time = time.time()
                        if current_time - last_db_update_time > 1.0 or bytes_copied == file_size:
                            async with get_db() as db:
                                await db.execute(
                                    "UPDATE queue SET bytes_transferred = ? WHERE id = ? AND status = 'running'",
                                    (bytes_copied, task_id),
                                )
                                await db.commit()
                            last_db_update_time = current_time

                        if (progress_pct % 10 == 0 and progress_pct > 0) or bytes_copied == file_size:
                            await broadcast("queue_progress", {
                                "task_id": task_id,
                                "bytes_transferred": bytes_copied,
                                "total_bytes": file_size,
                                "progress_pct": progress_pct,
                            })

            self._begin_irreversible_commit(task_id)
            await self._set_operation_phase(task_id, "committing")
            file_hash = hasher.hexdigest()
            now = datetime.now(timezone.utc).isoformat()
            src_stat = src_path.stat()
            os.utime(staging_path, (src_stat.st_atime, src_stat.st_mtime))
            os.replace(staging_path, dst_path)
            dst_stat = dst_path.stat()

            async with get_db() as db:
                await db.execute(
                    """
                    UPDATE file_index SET hash = ?, hash_computed_at = ?
                    WHERE side = ? AND relpath = ?
                    """,
                    (file_hash, now, task["src_side"], task["src_relpath"]),
                )
                await db.execute(
                    """
                    INSERT OR REPLACE INTO file_index
                    (side, relpath, size, mtime_ns, hash, hash_computed_at, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task["dst_side"], task["dst_relpath"], dst_stat.st_size,
                        dst_stat.st_mtime_ns, file_hash, now, now,
                    ),
                )
                await db.commit()

            print(f"Copied: {task['src_relpath']} → {task['dst_side']} (hash: {file_hash[:8]}...)")
        finally:
            if staging_path.exists():
                try:
                    staging_path.unlink()
                except OSError:
                    pass

    async def _execute_move(self, task: dict):
        """Execute or recover an atomic move within one storage side."""
        task_id = int(task["id"])
        src_root = self._get_root(task["src_side"])
        dst_root = self._get_root(task["dst_side"])
        if src_root != dst_root:
            raise ValueError("Move must be within the same side")

        src_path = self._resolve_path(task["src_side"], task["src_relpath"])
        dst_path = self._resolve_path(task["dst_side"], task["dst_relpath"])

        recovering_commit = (
            task.get("operation_phase") == "committing"
            and not src_path.exists()
            and dst_path.is_file()
        )

        if recovering_commit:
            execution = self._active.get(task_id)
            if execution:
                execution["commit_started"] = True
            print(f"Recovering committed move: {task['src_relpath']} → {task['dst_relpath']}")
        else:
            if not src_path.exists() or not src_path.is_file():
                raise FileNotFoundError(f"Source file not found: {src_path}")
            if dst_path.exists():
                raise FileExistsError(f"Destination already exists: {dst_path}")

            self._begin_irreversible_commit(task_id)
            await self._set_operation_phase(task_id, "committing")
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            await aiofiles.os.rename(src_path, dst_path)

        # Update index entry (preserve hash) and relpath-based source URLs
        now = datetime.now(timezone.utc).isoformat()
        dst_stat = dst_path.stat()
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT hash, hash_computed_at FROM file_index WHERE side = ? AND relpath = ?",
                (task["src_side"], task["src_relpath"]),
            )
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    """
                    UPDATE file_index
                    SET relpath = ?, size = ?, mtime_ns = ?, indexed_at = ?, hash = ?, hash_computed_at = ?
                    WHERE side = ? AND relpath = ?
                    """,
                    (
                        task["dst_relpath"],
                        dst_stat.st_size,
                        dst_stat.st_mtime_ns,
                        now,
                        row["hash"],
                        row["hash_computed_at"],
                        task["src_side"],
                        task["src_relpath"],
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO file_index (side, relpath, size, mtime_ns, hash, hash_computed_at, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task["src_side"],
                        task["dst_relpath"],
                        dst_stat.st_size,
                        dst_stat.st_mtime_ns,
                        None,
                        None,
                        now,
                    ),
                )

            # Migrate relpath-based source URL
            old_key = f"relpath:{task['src_relpath']}"
            new_key = f"relpath:{task['dst_relpath']}"
            await db.execute(
                "UPDATE source_urls SET key = ?, relpath = ? WHERE key = ?",
                (new_key, task["dst_relpath"], old_key),
            )
            await db.commit()

        print(f"Moved: {task['src_relpath']} → {task['dst_relpath']} ({task['src_side']})")
    
    async def _execute_delete(self, task: dict):
        """Execute a delete task."""
        task_id = int(task["id"])
        self._raise_if_cancelled(task_id)
        filepath = self._resolve_path(task["dst_side"], task["dst_relpath"])
        
        if not filepath.exists():
            print(f"File already deleted: {filepath}")
            return

        if not filepath.is_file():
            raise ValueError(f"Delete target is not a file: {filepath}")
        self._begin_irreversible_commit(task_id)
        await self._set_operation_phase(task_id, "committing")
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
            self._raise_if_cancelled(int(task_id))
                
            file_relpath = row["relpath"]
            local_path = self._resolve_path("local", file_relpath)
            lake_path = self._resolve_path("lake", file_relpath)
            
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
                            self._raise_if_cancelled(int(task_id))
                            hasher.update(chunk)
                    local_hash = hasher.hexdigest()
                    updates.append(("local", local_hash))
                
                if not lake_hash and lake_path.exists():
                    hasher = blake3.blake3()
                    async with aiofiles.open(lake_path, 'rb') as f:
                        while chunk := await f.read(1024 * 1024):
                            self._raise_if_cancelled(int(task_id))
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

    async def _execute_hash_file(self, task: dict):
        """Execute a single file hash task."""
        import blake3
        
        relpath = task["src_relpath"]
        task_id = task["id"]
        
        print(f"Hashing file: {relpath}")
        
        local_path = self._resolve_path("local", relpath)
        lake_path = self._resolve_path("lake", relpath)
        
        now = datetime.now(timezone.utc).isoformat()
        computed_hash = None
        
        # Hash whichever side(s) exist
        for side, path in [("local", local_path), ("lake", lake_path)]:
            self._raise_if_cancelled(int(task_id))
            if path.exists():
                # Check if already hashed
                async with get_db() as db:
                    cursor = await db.execute(
                        "SELECT hash FROM file_index WHERE side = ? AND relpath = ?",
                        (side, relpath)
                    )
                    row = await cursor.fetchone()
                    if row and row["hash"]:
                        computed_hash = row["hash"]
                        print(f"  {side}: already hashed ({computed_hash[:8]}...)")
                        continue
                
                # Hash the file
                hasher = blake3.blake3()
                file_size = path.stat().st_size
                bytes_read = 0
                last_db_update_time = 0.0

                async with get_db() as db:
                    await db.execute(
                        "UPDATE queue SET size_bytes = ?, bytes_transferred = 0 WHERE id = ?",
                        (file_size, task_id)
                    )
                    await db.commit()
                
                async with aiofiles.open(path, 'rb') as f:
                    while chunk := await f.read(1024 * 1024):
                        self._raise_if_cancelled(int(task_id))
                        hasher.update(chunk)
                        bytes_read += len(chunk)
                        
                        # Progress update
                        if file_size > 0:
                            pct = int((bytes_read / file_size) * 100)
                            now_ts = time.time()
                            if now_ts - last_db_update_time > 1.0 or bytes_read == file_size:
                                async with get_db() as db:
                                    await db.execute(
                                        "UPDATE queue SET bytes_transferred = ? WHERE id = ?",
                                        (bytes_read, task_id)
                                    )
                                    await db.commit()
                                last_db_update_time = now_ts
                            if pct % 20 == 0 or bytes_read == file_size:
                                await broadcast("queue_progress", {
                                    "task_id": task_id,
                                    "bytes_transferred": bytes_read,
                                    "total_bytes": file_size,
                                    "progress_pct": pct,
                                })
                
                computed_hash = hasher.hexdigest()
                
                # Update database
                async with get_db() as db:
                    await db.execute(
                        "UPDATE file_index SET hash = ?, hash_computed_at = ? WHERE side = ? AND relpath = ?",
                        (computed_hash, now, side, relpath)
                    )
                    await db.commit()
                
                print(f"  {side}: hashed ({computed_hash[:8]}...)")
        
        # Migrate any relpath-based source URL to hash-based
        if computed_hash:
            from app.services.source_manager import get_source_manager
            source_mgr = get_source_manager()
            await source_mgr.migrate_relpath_to_hash(relpath, computed_hash)
            
            # Broadcast that this file now has a hash (UI can update)
            await broadcast("file_hashed", {
                "relpath": relpath,
                "hash": computed_hash,
            })
        
        print(f"Hash complete: {relpath}")



# Convenience functions
def get_worker() -> QueueWorker:
    return QueueWorker.get_instance()
