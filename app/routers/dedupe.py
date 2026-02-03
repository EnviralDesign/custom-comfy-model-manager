"""Dedupe API endpoints for duplicate detection and removal."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal

from app.services.dedupe import DedupeService, DuplicateGroup

router = APIRouter()


class ScanRequest(BaseModel):
    side: Literal["local", "lake"]


class ScanResponse(BaseModel):
    scan_id: str
    side: str
    total_files: int
    duplicate_groups: int
    duplicate_files: int
    reclaimable_bytes: int


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
    This computes hashes for all files and groups by hash.
    """
    dedupe_service = DedupeService()
    result = await dedupe_service.scan(request.side)
    return result


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


@router.delete("/scan/{scan_id}")
async def clear_scan(scan_id: str):
    """Clear a scan's results from the database."""
    dedupe_service = DedupeService()
    await dedupe_service.clear_scan(scan_id)
    return {"status": "cleared"}
