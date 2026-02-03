"""Queue API endpoints for transfer operations."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal

from app.services.queue import QueueService, QueueTask

router = APIRouter()


class CopyRequest(BaseModel):
    src_side: Literal["local", "lake"]
    src_relpath: str
    dst_side: Literal["local", "lake"]
    dst_relpath: str | None = None  # If None, uses same relpath


class DeleteRequest(BaseModel):
    side: Literal["local", "lake"]
    relpath: str


class MirrorPlanRequest(BaseModel):
    src_side: Literal["local", "lake"]
    src_folder: str
    dst_side: Literal["local", "lake"]
    dst_folder: str | None = None  # If None, uses same folder


class MirrorPlan(BaseModel):
    copies: list[dict]
    deletes: list[dict]
    conflicts: list[dict]
    total_copy_bytes: int
    total_delete_bytes: int


class MirrorExecuteRequest(BaseModel):
    plan: MirrorPlan
    skip_deletes: bool = False


@router.get("/", response_model=list[QueueTask])
async def get_queue():
    """Get all queue tasks."""
    queue_service = QueueService()
    return await queue_service.get_all_tasks()


@router.get("/active", response_model=QueueTask | None)
async def get_active_task():
    """Get the currently running task, if any."""
    queue_service = QueueService()
    return await queue_service.get_active_task()


@router.post("/copy")
async def enqueue_copy(request: CopyRequest):
    """Enqueue a copy task."""
    if request.src_side == request.dst_side:
        raise HTTPException(400, "Cannot copy to the same side")
    
    queue_service = QueueService()
    task_id = await queue_service.enqueue_copy(
        src_side=request.src_side,
        src_relpath=request.src_relpath,
        dst_side=request.dst_side,
        dst_relpath=request.dst_relpath or request.src_relpath,
    )
    return {"task_id": task_id, "status": "queued"}


@router.post("/delete")
async def enqueue_delete(request: DeleteRequest):
    """
    Enqueue a delete task.
    Respects allow-delete policy for sync operations.
    """
    queue_service = QueueService()
    task_id = await queue_service.enqueue_delete(
        side=request.side,
        relpath=request.relpath,
        respect_policy=True,
    )
    return {"task_id": task_id, "status": "queued"}


@router.post("/pause")
async def pause_queue():
    """Pause queue processing."""
    queue_service = QueueService()
    await queue_service.pause()
    return {"status": "paused"}


@router.post("/resume")
async def resume_queue():
    """Resume queue processing."""
    queue_service = QueueService()
    await queue_service.resume()
    return {"status": "resumed"}


@router.post("/cancel/{task_id}")
async def cancel_task(task_id: int):
    """Cancel a specific task."""
    queue_service = QueueService()
    success = await queue_service.cancel_task(task_id)
    if not success:
        raise HTTPException(404, "Task not found or already completed")
    return {"status": "cancelled"}


@router.delete("/{task_id}")
async def remove_task(task_id: int):
    """Remove a pending task from the queue."""
    queue_service = QueueService()
    success = await queue_service.remove_task(task_id)
    if not success:
        raise HTTPException(404, "Task not found or not pending")
    return {"status": "removed"}


@router.post("/mirror/plan", response_model=MirrorPlan)
async def create_mirror_plan(request: MirrorPlanRequest):
    """
    Generate a mirror plan (preview).
    Shows what would be copied, deleted, and any conflicts.
    """
    queue_service = QueueService()
    plan = await queue_service.create_mirror_plan(
        src_side=request.src_side,
        src_folder=request.src_folder,
        dst_side=request.dst_side,
        dst_folder=request.dst_folder or request.src_folder,
    )
    return plan


@router.post("/mirror/execute")
async def execute_mirror(request: MirrorExecuteRequest):
    """Execute a mirror plan, enqueuing all tasks."""
    queue_service = QueueService()
    task_ids = await queue_service.execute_mirror_plan(
        plan=request.plan,
        skip_deletes=request.skip_deletes,
    )
    return {"tasks_enqueued": len(task_ids), "task_ids": task_ids}
