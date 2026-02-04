"""API router for manual tool testing."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional

from app.config import get_settings
from app.services.agent_tools import (
    civitai_search,
    civitai_model_version,
    civitai_by_hash,
    hf_search,
    hf_model_info,
    hf_resolve,
    url_validate,
)

router = APIRouter()


class CivitaiSearchRequest(BaseModel):
    query: str
    limit: int = Field(default=20, ge=1, le=50)
    page: Optional[int] = Field(default=1, ge=1, le=100)
    cursor: Optional[str] = None
    types: Optional[str] = None
    supports_generation: Optional[bool] = None
    primary_file_only: Optional[bool] = None
    nsfw: Optional[bool] = None
    tag: Optional[str] = None


class CivitaiModelVersionRequest(BaseModel):
    id: int


class CivitaiByHashRequest(BaseModel):
    hash: str


class HfSearchRequest(BaseModel):
    query: str
    limit: int = Field(default=20, ge=1, le=50)


class HfModelInfoRequest(BaseModel):
    repo_id: str


class HfResolveRequest(BaseModel):
    repo_id: str
    file: str
    revision: Optional[str] = None
    validate: bool = True


class UrlValidateRequest(BaseModel):
    url: HttpUrl


@router.post("/api/agent-tools/civitai/search")
async def api_civitai_search(request: CivitaiSearchRequest):
    settings = get_settings()
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="query is required")
    return civitai_search(
        query=request.query.strip(),
        limit=request.limit,
        page=request.page,
        cursor=request.cursor,
        types=request.types,
        supports_generation=request.supports_generation,
        primary_file_only=request.primary_file_only,
        nsfw=request.nsfw,
        tag=request.tag,
        base_url=settings.civitai_api_base_url,
        api_key=settings.civitai_api_key,
    )


@router.post("/api/agent-tools/civitai/model-version")
async def api_civitai_model_version(request: CivitaiModelVersionRequest):
    settings = get_settings()
    return civitai_model_version(
        version_id=request.id,
        base_url=settings.civitai_api_base_url,
        api_key=settings.civitai_api_key,
    )


@router.post("/api/agent-tools/civitai/by-hash")
async def api_civitai_by_hash(request: CivitaiByHashRequest):
    settings = get_settings()
    if not request.hash.strip():
        raise HTTPException(status_code=400, detail="hash is required")
    return civitai_by_hash(
        file_hash=request.hash.strip(),
        base_url=settings.civitai_api_base_url,
        api_key=settings.civitai_api_key,
    )


@router.post("/api/agent-tools/hf/search")
async def api_hf_search(request: HfSearchRequest):
    settings = get_settings()
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="query is required")
    return hf_search(
        query=request.query.strip(),
        limit=request.limit,
        api_key=settings.huggingface_api_key,
    )


@router.post("/api/agent-tools/hf/model-info")
async def api_hf_model_info(request: HfModelInfoRequest):
    settings = get_settings()
    if not request.repo_id.strip():
        raise HTTPException(status_code=400, detail="repo_id is required")
    return hf_model_info(
        repo_id=request.repo_id.strip(),
        api_key=settings.huggingface_api_key,
    )


@router.post("/api/agent-tools/hf/resolve")
async def api_hf_resolve(request: HfResolveRequest):
    settings = get_settings()
    if not request.repo_id.strip() or not request.file.strip():
        raise HTTPException(status_code=400, detail="repo_id and file are required")
    return hf_resolve(
        repo_id=request.repo_id.strip(),
        file_name=request.file.strip(),
        revision=request.revision,
        validate=request.validate,
        api_key=settings.huggingface_api_key,
    )


@router.post("/api/agent-tools/url/validate")
async def api_url_validate(request: UrlValidateRequest):
    return url_validate(url=str(request.url))
