"""Civitai API client helpers for model/file discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable

import requests


@dataclass
class CivitaiFileCandidate:
    download_url: str
    file_name: str | None
    model_id: int | None
    model_name: str | None
    version_id: int | None
    version_name: str | None
    metadata: dict[str, Any]


class CivitaiClient:
    def __init__(self, *, base_url: str, api_key: str | None = None, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "ComfyModelManager/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        url = f"{self.base_url}{path}"
        resp = self._session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def search_models(
        self,
        *,
        query: str,
        limit: int = 20,
        page: int | None = None,
        cursor: str | None = None,
        types: str | None = None,
        supports_generation: bool | None = None,
        primary_file_only: bool | None = None,
        nsfw: bool | None = None,
        tag: str | None = None,
    ) -> dict[str, Any] | None:
        params = {
            "query": query,
            "limit": limit,
            "sort": "Most Downloaded",
        }
        if types:
            params["types"] = types
        if supports_generation is not None:
            params["supportsGeneration"] = str(supports_generation).lower()
        if primary_file_only is not None:
            params["primaryFileOnly"] = str(primary_file_only).lower()
        if nsfw is not None:
            params["nsfw"] = str(nsfw).lower()
        if tag:
            params["tag"] = tag
        # Civitai does not allow page with query searches; use cursor instead.
        if cursor:
            params["cursor"] = cursor
        elif not query and page:
            params["page"] = page
        return self._get("/api/v1/models", params=params)

    def get_model_version(self, model_version_id: int) -> dict[str, Any] | None:
        return self._get(f"/api/v1/model-versions/{model_version_id}")

    def get_model_version_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        return self._get(f"/api/v1/model-versions/by-hash/{file_hash}")


def build_query_variants(filename: str) -> list[str]:
    stem = Path(filename).stem
    normalized = re.sub(r"[\W_]+", " ", stem).strip()

    # Remove common suffix tokens (fp, pruned/full, etc.)
    suffix_tokens = [
        "fp8",
        "fp16",
        "fp32",
        "f8",
        "f16",
        "f32",
        "pruned",
        "full",
        "safetensors",
        "ckpt",
        "model",
    ]

    tokens = normalized.split()
    while tokens and tokens[-1].lower() in suffix_tokens:
        tokens.pop()
    trimmed = " ".join(tokens).strip()

    # Drop trailing version like "v1.2" or "v2"
    trimmed = re.sub(r"\s+v?\d+(\.\d+)*$", "", trimmed).strip()

    variants = [stem, normalized, trimmed]

    # Add a shorter variant if still long
    if trimmed:
        words = trimmed.split()
        if len(words) > 3:
            variants.append(" ".join(words[:3]))

    seen: set[str] = set()
    result = []
    for v in variants:
        v = v.strip()
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        result.append(v)
    return result[:5]


def parse_filename_hints(filename: str) -> dict[str, str]:
    lower = filename.lower()
    hints: dict[str, str] = {}

    if lower.endswith(".safetensors"):
        hints["format"] = "SafeTensor"
    elif lower.endswith(".ckpt"):
        hints["format"] = "PickleTensor"

    if "pruned" in lower:
        hints["size"] = "pruned"
    elif "full" in lower:
        hints["size"] = "full"

    if "fp8" in lower or "f8" in lower:
        hints["fp"] = "fp8"
    elif "fp16" in lower or "f16" in lower:
        hints["fp"] = "fp16"
    elif "fp32" in lower or "f32" in lower:
        hints["fp"] = "fp32"

    return hints


def _metadata_matches(metadata: dict[str, Any], hints: dict[str, str]) -> bool:
    if not hints:
        return True
    for key, value in hints.items():
        meta_val = metadata.get(key)
        if meta_val is None:
            continue
        if str(meta_val).lower() != value.lower():
            return False
    return True


def _extract_file_candidates(
    model: dict[str, Any] | None,
    version: dict[str, Any],
) -> Iterable[CivitaiFileCandidate]:
    model_id = model.get("id") if model else None
    model_name = model.get("name") if model else None
    version_id = version.get("id")
    version_name = version.get("name")

    files = version.get("files") or []
    if isinstance(files, list):
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            download_url = file_entry.get("downloadUrl") or version.get("downloadUrl")
            if not download_url:
                continue
            yield CivitaiFileCandidate(
                download_url=download_url,
                file_name=file_entry.get("name"),
                model_id=model_id,
                model_name=model_name,
                version_id=version_id,
                version_name=version_name,
                metadata=file_entry.get("metadata") or {},
            )
    else:
        download_url = version.get("downloadUrl")
        if download_url:
            yield CivitaiFileCandidate(
                download_url=download_url,
                file_name=None,
                model_id=model_id,
                model_name=model_name,
                version_id=version_id,
                version_name=version_name,
                metadata={},
            )


def find_civitai_download(
    *,
    filename: str,
    file_hash: str | None,
    base_url: str,
    api_key: str | None,
    max_models_per_query: int = 6,
) -> dict[str, Any]:
    steps: list[str] = []
    client = CivitaiClient(base_url=base_url, api_key=api_key)
    hints = parse_filename_hints(filename)

    def choose_best(candidates: list[CivitaiFileCandidate]) -> CivitaiFileCandidate | None:
        if not candidates:
            return None
        exact = [c for c in candidates if c.file_name == filename]
        if exact:
            return exact[0]
        ci = [c for c in candidates if c.file_name and c.file_name.lower() == filename.lower()]
        if ci:
            return ci[0]
        return None

    if file_hash and not file_hash.startswith("fast:"):
        steps.append("Civitai hash lookup via model-versions/by-hash.")
        payload = client.get_model_version_by_hash(file_hash)
        if isinstance(payload, dict):
            version = payload.get("modelVersion") or payload
            if isinstance(version, dict):
                candidates = list(_extract_file_candidates(payload.get("model"), version))
                candidates = [c for c in candidates if _metadata_matches(c.metadata, hints)]
                best = choose_best(candidates)
                if best:
                    return {
                        "found": True,
                        "url": best.download_url,
                        "source": "civitai_hash",
                        "notes": f"Matched hash on model version {best.version_id}.",
                        "steps": steps,
                    }

    variants = build_query_variants(filename)
    steps.append(f"Civitai search variants: {', '.join(variants)}")

    for variant in variants:
        payload = client.search_models(query=variant, limit=20, page=1)
        if not payload:
            continue
        items = payload.get("items") or []
        if not isinstance(items, list):
            continue

        for model in items[:max_models_per_query]:
            if not isinstance(model, dict):
                continue
            model_versions = model.get("modelVersions") or []
            candidates: list[CivitaiFileCandidate] = []

            for version in model_versions:
                if not isinstance(version, dict):
                    continue
                if not version.get("files"):
                    version_id = version.get("id")
                    if isinstance(version_id, int):
                        detailed = client.get_model_version(version_id)
                        if isinstance(detailed, dict):
                            version = detailed.get("modelVersion") or detailed
                candidates.extend(_extract_file_candidates(model, version))

            candidates = [c for c in candidates if _metadata_matches(c.metadata, hints)]
            best = choose_best(candidates)
            if best:
                return {
                    "found": True,
                    "url": best.download_url,
                    "source": "civitai_search",
                    "notes": f"Matched via query '{variant}' (model {best.model_id}).",
                    "steps": steps,
                }

    return {"found": False, "url": None, "source": None, "notes": None, "steps": steps}
