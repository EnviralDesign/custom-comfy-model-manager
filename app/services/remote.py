"""Service for managing remote sessions."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
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
            
        task = RemoteTask(
            type=task_create.type, 
            payload=task_create.payload,
            label=label or task_create.type
        )
        self._tasks.append(task)
        self._task_event.set() # Wake up poller
        return task

    def update_task_progress(self, update: TaskProgressUpdate):
        """Update a task's status from the agent."""
        for t in self._tasks:
            if t.id == update.task_id:
                t.status = update.status
                if update.progress is not None:
                    t.progress = update.progress
                if update.message is not None:
                    t.message = update.message
                if update.error:
                    t.error = update.error
                
                # Timestamps
                if update.status == "running" and not t.started_at:
                    t.started_at = datetime.utcnow()
                if update.status in ["completed", "failed", "cancelled"] and not t.completed_at:
                    t.completed_at = datetime.utcnow()
                return

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
