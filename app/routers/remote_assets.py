"""API Router for Remote Assets (Serving & Resolution)."""

from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from typing import List, Optional

from app.services.remote import get_session_manager
from app.services.source_manager import get_source_manager
from app.config import get_settings
from app.dependencies import verify_remote_auth
from app.utils.streaming import range_requests_response

router = APIRouter()

class AssetSource(BaseModel):
    url: str
    type: str  # "web", "local", "lake"
    priority: int

class AssetResolution(BaseModel):
    hash: str
    relpath: Optional[str] = None
    sources: List[AssetSource]
    expected_size: Optional[int] = None


@router.post("/assets/resolve", dependencies=[Depends(verify_remote_auth)], response_model=AssetResolution)
async def resolve_asset(
    hash: str, 
    relpath: Optional[str] = None
):
    """
    Resolve a file hash (and optional relpath) to a list of download sources.
    Tiered: Web > Local > Lake.
    """
    settings = get_settings()
    source_mgr = get_source_manager()
    session_mgr = get_session_manager()
    
    # 1. Base Resolution
    sources = []
    
    # 2. Check Web Source (Metadata)
    meta = await source_mgr.get_source(hash)
    if meta:
        sources.append(AssetSource(url=meta.url, type="web", priority=1))
        
    # 3. Check Local & Lake Presence (via Index/Filesystem)
    # For now, we construct the URLs assuming the file exists at the given relpath.
    # In a real implementation, we should check if the file actually exists to avoid 404s.
    
    # We need the base URL from the session config or request
    # Since the agent connects to us, usng relative URLs or constructing full ones is tricky 
    # if we are behind a tunnel. The RemoteSessionManager knows the 'remote_base_url' from config.
    
    base_url = settings.remote_base_url.rstrip('/')
    
    if relpath:
        # Construct internal stream URLs
        # Note: We append the API Key to the query param? Or expect the agent to add the header?
        # The agent scripts usually use a shared session/header.
        # However, for simple downloading (wget/curl), query param is easier. 
        # But spec says "Bearer required". The agent must handle headers.
        
        # Local
        local_path = settings.local_models_root / relpath
        if local_path.exists():
            sources.append(AssetSource(
                url=f"{base_url}/api/remote/assets/file?side=local&relpath={relpath}",
                type="local",
                priority=2
            ))
            
        # Lake
        lake_path = settings.lake_models_root / relpath
        if lake_path.exists():
            sources.append(AssetSource(
                url=f"{base_url}/api/remote/assets/file?side=lake&relpath={relpath}",
                type="lake",
                priority=3
            ))

    return AssetResolution(
        hash=hash,
        relpath=relpath,
        sources=sources
    )


@router.get("/assets/file", dependencies=[Depends(verify_remote_auth)])
async def stream_file(
    request: Request,
    side: str = Query(..., pattern="^(local|lake)$"),
    relpath: str = Query(...)
):
    """
    Stream a file from Local or Lake storage.
    Supports Range header for resume.
    """
    settings = get_settings()
    
    # Security: Prevent traversal
    if ".." in relpath or relpath.startswith("/") or "\\" in relpath:
        raise HTTPException(status_code=400, detail="Invalid path")
        
    if side == "local":
        root = settings.local_models_root
    else:
        root = settings.lake_models_root
        
    file_path = (root / relpath).resolve()
    
    # Security: Ensure resolved path is inside root
    if not str(file_path).startswith(str(root.resolve())):
         raise HTTPException(status_code=403, detail="Path traversal detected")
         
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
        
    if not file_path.is_file():
         raise HTTPException(status_code=400, detail="Not a file")

    return range_requests_response(request, file_path)
