"""API router for the standalone downloader service."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional

from app.services.downloader import get_download_manager

router = APIRouter()


class DownloadRequest(BaseModel):
    url: HttpUrl
    filename: Optional[str] = None
    provider: Optional[str] = Field(default="auto", description="auto|civitai|huggingface|generic")
    api_key: Optional[str] = None
    start_now: bool = False


class DownloadJobResponse(BaseModel):
    id: int
    url: str
    filename: Optional[str]
    provider: str
    status: str
    bytes_downloaded: int
    total_bytes: Optional[int]
    created_at: str
    updated_at: str
    error_message: Optional[str]
    attempts: int
    dest_path: Optional[str]


class DownloadStartRequest(BaseModel):
    force: bool = True


@router.post("/api/downloader/jobs", response_model=DownloadJobResponse)
async def create_download_job(request: DownloadRequest):
    manager = get_download_manager()
    job = manager.create_job(
        url=str(request.url),
        filename=request.filename,
        provider=request.provider,
        api_key_override=request.api_key,
        start_now=request.start_now,
    )
    return DownloadJobResponse(**job.to_dict())


@router.get("/api/downloader/jobs", response_model=list[DownloadJobResponse])
async def list_download_jobs():
    manager = get_download_manager()
    return [DownloadJobResponse(**job.to_dict()) for job in manager.list_jobs()]


@router.post("/api/downloader/jobs/{job_id}/start")
async def start_download_job(job_id: int, request: DownloadStartRequest):
    manager = get_download_manager()
    if not manager.start_job(job_id, force=request.force):
        raise HTTPException(status_code=409, detail="Job could not be started")
    return {"status": "started", "force": request.force}


@router.post("/api/downloader/jobs/{job_id}/cancel")
async def cancel_download_job(job_id: int):
    manager = get_download_manager()
    if not manager.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "cancelled"}


@router.post("/api/downloader/jobs/cancel-all")
async def cancel_all_download_jobs():
    manager = get_download_manager()
    cancelled = manager.cancel_all()
    return {"status": "cancelled", "count": cancelled}
