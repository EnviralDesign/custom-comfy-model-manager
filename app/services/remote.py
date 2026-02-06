"""Service for managing remote sessions."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple
import asyncio

from app.config import get_settings
from app.schemas.remote_task import RemoteTask, RemoteTaskCreate, TaskProgressUpdate

class RemoteSessionManager:
    """
    Manages the state of the ephemeral remote session.
    Since we only support ONE remote session at a time, we can store state in memory.
    """
    
    def __init__(self):
        self._api_key: Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._agent_connected: bool = False
        self._agent_info: dict = {}
        self._last_heartbeat: Optional[datetime] = None
        
        # Task Queue
        self._tasks: List[RemoteTask] = []
        self._task_event = asyncio.Event() # For long-polling

    @property
    def is_active(self) -> bool:
        """Check if session is currently active and not expired."""
        if not self._api_key or not self._expires_at:
            return False
        
        # Check expiry
        if datetime.now(timezone.utc) > self._expires_at:
            self.end_session() # Cleanup if expired
            return False
            
        return True

    def enable_session(self) -> dict:
        """Start a new session."""
        settings = get_settings()
        
        # Generate high-entropy key
        self._api_key = secrets.token_urlsafe(32)
        
        # Set expiry
        ttl = getattr(settings, "remote_session_ttl_minutes", 60)
        self._expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl)
        
        # Reset agent state
        self._agent_connected = False
        self._agent_info = {}
        self._last_heartbeat = None
        
        return {
            "api_key": self._api_key,
            "expires_at": self._expires_at,
            "ttl_minutes": ttl
        }

    def end_session(self):
        """Terminate the current session."""
        self._api_key = None
        self._expires_at = None
        self._agent_connected = False
        self._agent_info = {}
        self._last_heartbeat = None
        self._tasks = [] # Clear tasks on end
        self._task_event = asyncio.Event()

    def validate_key(self, key: str) -> bool:
        """Validate a bearer token."""
        if not self.is_active:
            return False
        # Constant time comparison to prevent timing attacks
        return secrets.compare_digest(key, self._api_key)

    async def wait_for_task(self, timeout: float = 20.0) -> Optional[RemoteTask]:
        """
        Wait for a pending task. Used by agent long-polling.
        Returns the next pending task or None if timeout.
        """
        if not self.is_active:
            return None
            
        # Check immediate
        next_task = self._get_next_pending()
        if next_task:
            return next_task
            
        # Wait
        try:
            await asyncio.wait_for(self._task_event.wait(), timeout=timeout)
            self._task_event.clear()
            return self._get_next_pending()
        except asyncio.TimeoutError:
            return None

    def _get_next_pending(self) -> Optional[RemoteTask]:
        """Get the first PENDING task."""
        for t in self._tasks:
            if t.status == "pending":
                return t
        return None

    def enqueue_task(self, task_create: RemoteTaskCreate, label: str = "") -> RemoteTask:
        """Enqueue a new task."""
        if not self.is_active:
            raise ValueError("No active session")

        if task_create.type == "DOWNLOAD_URLS":
            return self._enqueue_or_merge_download_urls(task_create, label)

        task = RemoteTask(
            type=task_create.type, 
            payload=task_create.payload,
            label=label or task_create.type
        )
        self._tasks.append(task)
        self._task_event.set() # Wake up poller
        return task

    def _make_task(self, task_create: RemoteTaskCreate, label: str = "") -> RemoteTask:
        task = RemoteTask(
            type=task_create.type,
            payload=task_create.payload,
            label=label or task_create.type,
        )
        self._tasks.append(task)
        self._task_event.set()  # Wake up poller
        return task

    def _task_item_key(self, item: dict) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        relpath = item.get("relpath")
        if relpath:
            return f"relpath:{relpath}"
        url = item.get("url")
        if url:
            return f"url:{url}"
        return None

    def _active_download_tasks(self) -> Tuple[List[RemoteTask], List[RemoteTask]]:
        running = []
        pending = []
        for task in self._tasks:
            if task.type != "DOWNLOAD_URLS":
                continue
            if task.status == "running":
                running.append(task)
            elif task.status == "pending":
                pending.append(task)
        return running, pending

    def _enqueue_or_merge_download_urls(self, task_create: RemoteTaskCreate, label: str = "") -> RemoteTask:
        payload = task_create.payload or {}
        incoming_items = payload.get("items")
        if not isinstance(incoming_items, list):
            incoming_items = []

        # Normalize and dedupe within incoming list first.
        uniq_items: List[dict] = []
        uniq_keys = set()
        for item in incoming_items:
            key = self._task_item_key(item)
            if not key or key in uniq_keys:
                continue
            uniq_keys.add(key)
            uniq_items.append(item)

        running_tasks, pending_tasks = self._active_download_tasks()
        existing_keys = set()
        for task in [*running_tasks, *pending_tasks]:
            for item in (task.payload or {}).get("items", []) or []:
                key = self._task_item_key(item)
                if key:
                    existing_keys.add(key)

        new_items = [item for item in uniq_items if self._task_item_key(item) not in existing_keys]
        if not new_items:
            if pending_tasks:
                return pending_tasks[-1]
            if running_tasks:
                return running_tasks[-1]
            task_create.payload = {"items": uniq_items}
            return self._make_task(task_create, label)

        # Prefer appending to an existing pending queue extension if present.
        if pending_tasks:
            target = pending_tasks[-1]
            if not isinstance(target.payload, dict):
                target.payload = {}
            existing = target.payload.get("items")
            if not isinstance(existing, list):
                existing = []
            existing.extend(new_items)
            target.payload["items"] = existing
            target.label = target.label or label or target.type
            target.message = f"Queued {len(existing)} provision item(s)."
            return target

        # If a batch is currently running, enqueue a follow-up pending batch.
        if running_tasks:
            task_create.payload = {"items": new_items}
            return self._make_task(task_create, label or "Provision Queue Extension")

        # Fresh queue.
        task_create.payload = {"items": new_items}
        return self._make_task(task_create, label)

    def update_task_progress(self, update: TaskProgressUpdate):
        """Update a task's status from the agent."""
        for t in self._tasks:
            if t.id == update.task_id:
                # Ignore agent-side progress updates after a UI cancellation.
                if t.status == "cancelled" and update.status != "cancelled":
                    return
                t.status = update.status
                if update.progress is not None:
                    t.progress = update.progress
                if update.message is not None:
                    t.message = update.message
                if update.error:
                    t.error = update.error
                if update.meta:
                    if t.meta is None:
                        t.meta = {}
                    for key, value in update.meta.items():
                        if key == "items_status" and isinstance(value, dict):
                            existing = t.meta.get("items_status")
                            if isinstance(existing, dict):
                                existing.update(value)
                                t.meta["items_status"] = existing
                            else:
                                t.meta["items_status"] = value
                        else:
                            t.meta[key] = value
                
                # Timestamps
                if update.status == "running" and not t.started_at:
                    t.started_at = datetime.utcnow()
                if update.status in ["completed", "failed", "cancelled"] and not t.completed_at:
                    t.completed_at = datetime.utcnow()
                return

    def get_task(self, task_id: str) -> Optional[RemoteTask]:
        """Get a task by id."""
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending or running task."""
        task = self.get_task(task_id)
        if not task:
            return False
        if task.status in ["completed", "failed", "cancelled"]:
            return False
        task.status = "cancelled"
        task.message = "Cancelled by user."
        task.completed_at = datetime.utcnow()
        return True

    def get_tasks(self) -> List[RemoteTask]:
        """Get all tasks."""
        return self._tasks

    def get_status(self) -> dict:
        """Get current session status for UI."""
        return {
            "is_active": self.is_active,
            "expires_at": self._expires_at.isoformat() if self._expires_at else None,
            "api_key": self._api_key if self.is_active else None, 
            "agent_connected": self._agent_connected,
            "agent_info": self._agent_info,
            "last_heartbeat": self._last_heartbeat.isoformat() if self._last_heartbeat else None,
            "tasks_count": len(self._tasks),
            "pending_count": len([t for t in self._tasks if t.status == "pending"])
        }

    def register_agent(self, info: dict):
        """Register the connected agent."""
        if not self.is_active:
            raise ValueError("No active session")
        self._agent_connected = True
        self._agent_info = info
        self.heartbeat()

    def heartbeat(self):
        """Update last heartbeat."""
        if not self.is_active:
            return
        self._last_heartbeat = datetime.now(timezone.utc)


# Global instance
_session_manager = RemoteSessionManager()

def get_session_manager() -> RemoteSessionManager:
    return _session_manager
