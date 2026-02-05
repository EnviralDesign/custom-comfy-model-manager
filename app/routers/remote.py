"""API Router for Remote Session management."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional, List

from app.services.remote import get_session_manager, RemoteSessionManager
from app.config import get_settings
from app.dependencies import verify_remote_auth
from app.schemas.remote_task import RemoteTask, RemoteTaskCreate, TaskProgressUpdate

router = APIRouter()

# --- Schemas ---

class SessionStatusResponse(BaseModel):
    is_active: bool
    expires_at: Optional[str]
    api_key: Optional[str]
    agent_connected: bool
    agent_info: dict
    last_heartbeat: Optional[str]
    remote_base_url: str
    torch_index_url: str
    torch_index_flag: str
    torch_packages: List[str]

class EnableSessionResponse(BaseModel):
    api_key: str
    expires_at: str
    ttl_minutes: int

class AgentRegisterRequest(BaseModel):
    hostname: str
    os: str
    details: Optional[dict] = {}


# --- Endpoints ---

@router.get("/status", response_model=SessionStatusResponse)
async def get_status():
    """Get the current status of the remote session."""
    mgr = get_session_manager()
    status_dict = mgr.get_status()
    
    settings = get_settings()
    status_dict["remote_base_url"] = settings.remote_base_url
    status_dict["torch_index_url"] = settings.remote_torch_index_url
    status_dict["torch_index_flag"] = settings.remote_torch_index_flag
    status_dict["torch_packages"] = settings.remote_torch_packages.split()
    
    return status_dict


@router.post("/session/enable", response_model=EnableSessionResponse)
async def enable_session():
    """Enable a new remote session."""
    mgr = get_session_manager()
    result = mgr.enable_session()
    
    # Determine basic return structure
    return {
        "api_key": result["api_key"],
        "expires_at": result["expires_at"].isoformat(),
        "ttl_minutes": result["ttl_minutes"]
    }


@router.post("/session/end")
async def end_session():
    """End the current session."""
    mgr = get_session_manager()
    mgr.end_session()
    return {"status": "ended"}


# --- Agent Endpoints (Authenticated) ---

@router.post("/agent/register", dependencies=[Depends(verify_remote_auth)])
async def register_agent(req: AgentRegisterRequest):
    """Register the remote agent."""
    mgr = get_session_manager()
    
    info = {
        "hostname": req.hostname,
        "os": req.os,
        **req.details
    }
    mgr.register_agent(info)
    return {"status": "registered"}


@router.post("/agent/heartbeat", dependencies=[Depends(verify_remote_auth)])
async def agent_heartbeat():
    """Keep the session and agent connection alive."""
    mgr = get_session_manager()
    mgr.heartbeat()
    return {"status": "ok"}


# --- Task Endpoints (Agent) ---

@router.get("/tasks/next", response_model=Optional[RemoteTask], dependencies=[Depends(verify_remote_auth)])
async def get_next_task():
    """Long-poll for the next pending task."""
    mgr = get_session_manager()
    task = await mgr.wait_for_task(timeout=20.0)
    return task

@router.post("/tasks/progress", dependencies=[Depends(verify_remote_auth)])
async def update_progress(update: TaskProgressUpdate):
    """Update task progress."""
    mgr = get_session_manager()
    mgr.update_task_progress(update)
    return {"status": "updated"}


# --- Task Endpoints (UI) ---

@router.get("/tasks", response_model=List[RemoteTask])
async def list_tasks():
    """List all tasks in the session."""
    mgr = get_session_manager()
    return mgr.get_tasks()

@router.post("/tasks/enqueue", response_model=RemoteTask)
async def enqueue_task(task: RemoteTaskCreate, label: str = ""):
    """Enqueue a task from the UI."""
    mgr = get_session_manager()
    return mgr.enqueue_task(task, label)
