"""Dedupe API endpoints for duplicate detection and removal."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal

from app.services.dedupe import DedupeService, DuplicateGroup

router = APIRouter()


class ScanRequest(BaseModel):
    side: Literal["local", "lake"]
    mode: Literal["full", "fast"] = "full"


class ScanResponse(BaseModel):
    task_id: int | None = None
    status: str | None = None
    scan_id: str | None = None
    side: str | None = None
    total_files: int | None = None
    duplicate_groups: int | None = None
    duplicate_files: int | None = None
    reclaimable_bytes: int | None = None


class KeepSelection(BaseModel):
    group_id: int
    keep_relpath: str


class ExecuteRequest(BaseModel):
    scan_id: str
    selections: list[KeepSelection]


@router.post("/scan", response_model=ScanResponse)
async def start_scan(request: ScanRequest):
    """
    Start a duplicate scan on one side.
    Enqueues a 'dedupe_scan' task.
    """
    dedupe_service = DedupeService()
    task_id = await dedupe_service.enqueue_scan(request.side, mode=request.mode)
    return {"task_id": task_id, "status": "queued"}


@router.get("/results/{scan_id}", response_model=list[DuplicateGroup])
async def get_results(scan_id: str):
    """Get the duplicate groups from a scan."""
    dedupe_service = DedupeService()
    groups = await dedupe_service.get_groups(scan_id)
    if not groups:
        raise HTTPException(404, "Scan not found or no duplicates")
    return groups


@router.post("/execute")
async def execute_dedupe(request: ExecuteRequest):
    """
    Execute the dedupe operation.
    Deletes all files except the selected 'keep' file in each group.
    NOTE: Dedupe deletion IGNORES allow-delete policy (always allowed).
    """
    dedupe_service = DedupeService()
    result = await dedupe_service.execute(
        scan_id=request.scan_id,
        selections=request.selections,
    )
    return result


@router.get("/scan/latest", response_model=ScanResponse | None)
async def get_latest_scan(side: str | None = None):
    """Retrieve the results of the most recent completed scan."""
    dedupe_service = DedupeService()
    result = await dedupe_service.get_latest_scan(side)
    if not result:
        return None
    return result


@router.delete("/scan/{scan_id}")
async def clear_scan(scan_id: str):
    """Clear a scan's results from the database."""
    dedupe_service = DedupeService()
    await dedupe_service.clear_scan(scan_id)
    return {"status": "cleared"}
