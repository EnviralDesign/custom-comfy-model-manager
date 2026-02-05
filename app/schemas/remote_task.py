"""Remote Task schemas."""

from pydantic import BaseModel, Field
from typing import Literal, Optional, Any
from uuid import uuid4, UUID
from datetime import datetime

TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
TaskType = Literal[
    "COMFY_GIT_CLONE",
    "CREATE_VENV",
    "ASSET_DOWNLOAD",
    "DOWNLOAD_URLS",
    "PIP_INSTALL_TORCH",
    "PIP_INSTALL_REQUIREMENTS",
    "INSTALL_COMFYUI_MANAGER",
]

class RemoteTaskBase(BaseModel):
    type: TaskType
    payload: dict = Field(default_factory=dict)

class RemoteTaskCreate(RemoteTaskBase):
    pass

class RemoteTask(RemoteTaskBase):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: TaskStatus = "pending"
    progress: float = 0.0
    message: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    
    # Metadata for UI
    label: str = "" 
    meta: dict = Field(default_factory=dict)

class TaskProgressUpdate(BaseModel):
    task_id: str
    status: TaskStatus
    progress: Optional[float] = None
    message: Optional[str] = None
    error: Optional[str] = None
    meta: Optional[dict] = None
