"""Reusable tool implementations for the agent and manual testing."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from app.services.civitai_api import CivitaiClient
from app.services.url_utils import check_url_sync


def _hf_get(path: str, *, api_key: str | None, params: dict[str, Any] | None = None) -> dict | list | None:
    headers = {"User-Agent": "ComfyModelManager/0.1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"https://huggingface.co{path}"
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    if resp.status_code >= 400:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _summarize_hf_models(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    summarized = []
    for item in items[:limit]:
        summarized.append(
            {
                "id": item.get("id") or item.get("modelId"),
                "author": item.get("author"),
                "likes": item.get("likes"),
                "downloads": item.get("downloads"),
                "lastModified": item.get("lastModified"),
                "pipeline_tag": item.get("pipeline_tag"),
            }
        )
    return summarized


def _summarize_civitai_models(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    summarized = []
    for model in items[:limit]:
        summarized.append(
            {
                "id": model.get("id"),
                "name": model.get("name"),
                "type": model.get("type"),
                "nsfw": model.get("nsfw"),
                "modelVersions": [
                    {
                        "id": mv.get("id"),
                        "name": mv.get("name"),
                        "files": [
                            {
                                "name": f.get("name"),
                                "downloadUrl": f.get("downloadUrl"),
                                "metadata": f.get("metadata"),
                            }
                            for f in (mv.get("files") or [])[:4]
                            if isinstance(f, dict)
                        ],
                    }
                    for mv in (model.get("modelVersions") or [])[:3]
                    if isinstance(mv, dict)
                ],
            }
        )
    return summarized


def civitai_search(
    *,
    query: str,
    limit: int,
    page: int | None,
    cursor: str | None,
    base_url: str,
    api_key: str | None,
    types: str | None = None,
    supports_generation: bool | None = None,
    primary_file_only: bool | None = None,
    nsfw: bool | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    limit = min(int(limit or 6), 20)
    page_val = max(1, int(page or 1)) if page is not None else None
    client = CivitaiClient(base_url=base_url, api_key=api_key)
    payload = client.search_models(
        query=query,
        limit=limit,
        page=page_val,
        cursor=cursor,
        types=types,
        supports_generation=supports_generation,
        primary_file_only=primary_file_only,
        nsfw=nsfw,
        tag=tag,
    ) or {}
    items = payload.get("items") or []
    return {
        "items": _summarize_civitai_models(items, limit),
        "nextPage": payload.get("nextPage"),
    }


def civitai_model_version(*, version_id: int, base_url: str, api_key: str | None) -> dict[str, Any]:
    client = CivitaiClient(base_url=base_url, api_key=api_key)
    payload = client.get_model_version(version_id) or {}
    version = payload.get("modelVersion") or payload
    files = []
    for f in (version.get("files") or [])[:8]:
        if not isinstance(f, dict):
            continue
        files.append(
            {
                "name": f.get("name"),
                "downloadUrl": f.get("downloadUrl"),
                "metadata": f.get("metadata"),
            }
        )
    return {"modelVersion": {"id": version.get("id"), "name": version.get("name"), "files": files}}


def civitai_by_hash(*, file_hash: str, base_url: str, api_key: str | None) -> dict[str, Any]:
    client = CivitaiClient(base_url=base_url, api_key=api_key)
    payload = client.get_model_version_by_hash(file_hash)
    if not payload:
        return {"found": False}
    version = payload.get("modelVersion") or payload
    model_obj = payload.get("model") or {}
    files = []
    for f in (version.get("files") or [])[:6]:
        if not isinstance(f, dict):
            continue
        files.append(
            {
                "name": f.get("name"),
                "downloadUrl": f.get("downloadUrl"),
                "metadata": f.get("metadata"),
            }
        )
    return {
        "found": bool(version),
        "model": {
            "id": model_obj.get("id"),
            "name": model_obj.get("name"),
            "type": model_obj.get("type"),
        },
        "modelVersion": {
            "id": version.get("id"),
            "name": version.get("name"),
            "files": files,
        },
    }


def hf_search(*, query: str, limit: int, api_key: str | None) -> dict[str, Any]:
    limit = min(int(limit or 6), 20)
    payload = _hf_get("/api/models", api_key=api_key, params={"search": query, "limit": limit}) or []
    if not isinstance(payload, list):
        return {"items": []}
    return {"items": _summarize_hf_models(payload, limit)}


def hf_model_info(*, repo_id: str, api_key: str | None) -> dict[str, Any]:
    payload = _hf_get(f"/api/models/{repo_id}", api_key=api_key) or {}
    siblings = payload.get("siblings") or []
    files = []
    allowed_ext = (".safetensors", ".ckpt", ".pt", ".bin")
    for item in siblings:
        if not isinstance(item, dict):
            continue
        name = item.get("rfilename")
        if not name:
            continue
        if name.lower().endswith(allowed_ext):
            files.append(name)
    return {"repo_id": repo_id, "files": files[:60]}


def hf_resolve(
    *,
    repo_id: str,
    file_name: str,
    revision: str | None,
    validate: bool,
    api_key: str | None,
) -> dict[str, Any]:
    revision = revision or "main"
    safe_file = quote(file_name, safe="/")
    url = f"https://huggingface.co/{repo_id}/resolve/{revision}/{safe_file}"
    if validate:
        validation = check_url_sync(url)
        return {"url": url, "validation": validation}
    return {"url": url}


def url_validate(*, url: str) -> dict[str, Any]:
    return check_url_sync(url)
