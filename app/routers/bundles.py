"""API Router for Bundle Management."""

from typing import List, Optional
import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.bundle_service import get_bundle_service, Bundle, ResolvedAsset, BundleCustomNode
from app.config import get_settings

router = APIRouter()


class CreateBundleRequest(BaseModel):
    name: str
    description: Optional[str] = None


class UpdateBundleRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class AddAssetRequest(BaseModel):
    root_type: str = "models"
    relpath: str
    hash: Optional[str] = None
    source_url_override: Optional[str] = None


class AddCustomNodeRequest(BaseModel):
    install_type: str = "registry"
    node_id: str
    name: Optional[str] = None
    repository: Optional[str] = None
    version: Optional[str] = None


class ResolveBundlesRequest(BaseModel):
    bundle_names: List[str]


class BundleListResponse(BaseModel):
    bundles: List[Bundle]


class ResolvedBundleResponse(BaseModel):
    assets: List[ResolvedAsset]
    custom_nodes: List[BundleCustomNode] = []
    total_size: Optional[int] = None


@router.get("/bundles", response_model=BundleListResponse)
async def list_bundles():
    """List all bundles."""
    service = get_bundle_service()
    bundles = await service.list_bundles()
    return BundleListResponse(bundles=bundles)


@router.post("/bundles", response_model=Bundle)
async def create_bundle(request: CreateBundleRequest):
    """Create a new bundle."""
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Bundle name cannot be empty")
    
    service = get_bundle_service()
    
    # Check if name already exists
    existing = await service.get_bundle(request.name)
    if existing:
        raise HTTPException(status_code=409, detail="Bundle with this name already exists")
    
    return await service.create_bundle(request.name.strip(), request.description)


@router.get("/bundles/{name}", response_model=Bundle)
async def get_bundle(name: str):
    """Get a bundle by name."""
    service = get_bundle_service()
    bundle = await service.get_bundle(name)
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return bundle


@router.put("/bundles/{name}", response_model=Bundle)
async def update_bundle(name: str, request: UpdateBundleRequest):
    """Update a bundle's metadata."""
    service = get_bundle_service()
    bundle = await service.update_bundle(name, request.name, request.description)
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return bundle


@router.delete("/bundles/{name}")
async def delete_bundle(name: str):
    """Delete a bundle."""
    service = get_bundle_service()
    success = await service.delete_bundle(name)
    if not success:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return {"status": "deleted", "name": name}


@router.post("/bundles/{name}/assets")
async def add_asset(name: str, request: AddAssetRequest):
    """Add an asset to a bundle."""
    if not request.relpath.strip():
        raise HTTPException(status_code=400, detail="Asset relpath cannot be empty")
    
    service = get_bundle_service()
    success = await service.add_asset(
        name, 
        request.relpath.strip(), 
        request.hash, 
        request.source_url_override,
        request.root_type,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return {"status": "added", "root_type": request.root_type, "relpath": request.relpath}


@router.post("/bundles/{name}/assets/folder")
async def add_folder_assets(name: str, folder_path: str, root_type: str = "models"):
    """Add all assets in a folder to a bundle."""
    service = get_bundle_service()
    count = await service.add_folder(name, folder_path, root_type)
    return {"status": "added", "count": count}


@router.delete("/bundles/{name}/assets/{relpath:path}")
async def remove_asset(name: str, relpath: str, root_type: str = "models"):
    """Remove an asset from a bundle."""
    service = get_bundle_service()
    success = await service.remove_asset(name, relpath, root_type)
    if not success:
        raise HTTPException(status_code=404, detail="Bundle or asset not found")
    return {"status": "removed", "root_type": root_type, "relpath": relpath}


@router.get("/bundles/registry/search")
async def search_registry_nodes(q: str, limit: int = 20):
    """Search the official Comfy Registry for custom node packs."""
    query = q.strip()
    if not query:
        return {"nodes": []}
    limit = max(1, min(limit, 50))
    try:
        resp = requests.get(
            "https://api.comfy.org/nodes",
            params={"search": query, "limit": limit},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Registry search failed: {exc}") from exc

    nodes = []
    for item in payload.get("nodes", []) or []:
        latest = item.get("latest_version") or {}
        nodes.append({
            "id": item.get("id"),
            "name": item.get("name") or item.get("id"),
            "description": item.get("description") or "",
            "repository": item.get("repository") or "",
            "version": latest.get("version"),
            "downloads": item.get("downloads"),
            "github_stars": item.get("github_stars"),
        })
    return {"nodes": nodes}


@router.post("/bundles/{name}/custom-nodes")
async def add_custom_node(name: str, request: AddCustomNodeRequest):
    """Add a custom node pack to a bundle."""
    if not request.node_id.strip():
        raise HTTPException(status_code=400, detail="Custom node id cannot be empty")
    service = get_bundle_service()
    success = await service.add_custom_node(
        name,
        request.node_id.strip(),
        request.install_type,
        request.name,
        request.repository,
        request.version,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return {"status": "added", "install_type": request.install_type, "node_id": request.node_id}


@router.delete("/bundles/{name}/custom-nodes/{node_id:path}")
async def remove_custom_node(name: str, node_id: str, install_type: str = "registry"):
    """Remove a custom node pack from a bundle."""
    service = get_bundle_service()
    success = await service.remove_custom_node(name, install_type, node_id)
    if not success:
        raise HTTPException(status_code=404, detail="Bundle or custom node not found")
    return {"status": "removed", "install_type": install_type, "node_id": node_id}


@router.post("/bundles/resolve", response_model=ResolvedBundleResponse)
async def resolve_bundles(request: ResolveBundlesRequest, req: Request):
    """
    Resolve one or more bundles to a list of downloadable assets.
    Returns deduplicated list with best URLs for each asset.
    """
    if not request.bundle_names:
        raise HTTPException(status_code=400, detail="At least one bundle name required")
    
    # Build base URL for remote file serving.
    # Prefer configured tunnel URL so remote agents never receive localhost URLs.
    settings = get_settings()
    configured = (settings.remote_base_url or "").strip()
    if configured and "your.domain.example" not in configured:
        server_base_url = configured
    else:
        server_base_url = f"{req.url.scheme}://{req.url.netloc}"
    
    service = get_bundle_service()
    resolved = await service.resolve_bundles(request.bundle_names, server_base_url)
    custom_nodes = await service.resolve_custom_nodes(request.bundle_names)
    
    # Calculate total size
    total_size = sum(a.size or 0 for a in resolved)
    
    return ResolvedBundleResponse(
        assets=resolved,
        custom_nodes=custom_nodes,
        total_size=total_size if total_size > 0 else None,
    )
