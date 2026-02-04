"""API router for agent trace debugging."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.services.agent_trace import get_agent_trace_manager

router = APIRouter()


class AgentTraceRequest(BaseModel):
    query: str
    file_hash: Optional[str] = None
    relpath: Optional[str] = None
    require_exact_filename: bool = True
    max_steps: Optional[int] = Field(default=None, ge=1, le=50)


class AgentTraceJobResponse(BaseModel):
    id: int
    query: str
    filename: str
    file_hash: Optional[str]
    relpath: Optional[str]
    require_exact_filename: bool
    status: str
    created_at: str
    updated_at: str
    result: Optional[dict]
    error: Optional[str]
    trace: Optional[list[dict]]


@router.post("/api/agent-debug/jobs", response_model=AgentTraceJobResponse)
async def create_agent_trace_job(request: AgentTraceRequest):
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="query is required")
    manager = get_agent_trace_manager()
    job = manager.create_job(
        query=request.query.strip(),
        file_hash=request.file_hash,
        relpath=request.relpath,
        require_exact_filename=request.require_exact_filename,
        max_steps=request.max_steps,
    )
    return AgentTraceJobResponse(**job.to_dict())


@router.get("/api/agent-debug/jobs", response_model=list[AgentTraceJobResponse])
async def list_agent_trace_jobs():
    manager = get_agent_trace_manager()
    return [AgentTraceJobResponse(**job.to_dict(include_trace=False)) for job in manager.list_jobs()]


@router.get("/api/agent-debug/jobs/{job_id}", response_model=AgentTraceJobResponse)
async def get_agent_trace_job(job_id: int):
    manager = get_agent_trace_manager()
    job = manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return AgentTraceJobResponse(**job.to_dict())


@router.post("/api/agent-debug/jobs/{job_id}/cancel")
async def cancel_agent_trace_job(job_id: int):
    manager = get_agent_trace_manager()
    if not manager.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "cancelled"}
