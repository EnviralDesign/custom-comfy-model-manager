"""URL validation and filename utilities."""

from __future__ import annotations

from typing import Optional
from urllib.parse import unquote, urlparse

import requests

from app.config import get_settings


def _parse_content_disposition_filename(header_value: str | None) -> Optional[str]:
    if not header_value:
        return None
    header = header_value.strip()
    if "filename*=" in header:
        parts = header.split("filename*=", 1)[1]
        parts = parts.strip().strip(";")
        if "''" in parts:
            _, encoded = parts.split("''", 1)
        else:
            encoded = parts
        return unquote(encoded.strip().strip('"'))
    if "filename=" in header:
        parts = header.split("filename=", 1)[1]
        return parts.strip().strip(";").strip('"')
    return None


def check_url_sync(url: str) -> dict:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        settings = get_settings()
        host = (urlparse(url).hostname or "").lower()
        if host.endswith("civitai.com") and settings.civitai_api_key:
            headers["Authorization"] = f"Bearer {settings.civitai_api_key}"
        elif (host.endswith("huggingface.co") or host.endswith("hf.co")) and settings.huggingface_api_key:
            headers["Authorization"] = f"Bearer {settings.huggingface_api_key}"

        response = requests.head(url, allow_redirects=True, timeout=10, headers=headers)

        # If 404 or other error, or if Content-Length is missing (some sites block HEAD), try GET
        if response.status_code != 200 or not response.headers.get("Content-Length"):
            response = requests.get(url, stream=True, timeout=10, headers=headers)
        elif host.endswith("civitai.com") and not response.headers.get("Content-Disposition"):
            # Some Civitai downloads only include filename on GET
            response = requests.get(url, stream=True, timeout=10, headers=headers)

        size = response.headers.get("Content-Length")
        content_type = response.headers.get("Content-Type", "").lower()
        cd_filename = _parse_content_disposition_filename(response.headers.get("Content-Disposition"))

        # Heuristic: if it's text/html, it's likely a landing page, not a direct download
        is_webpage = "text/html" in content_type

        return {
            "ok": response.status_code == 200 and not is_webpage,
            "status": response.status_code,
            "size": int(size) if size else None,
            "type": content_type,
            "url": response.url,
            "is_webpage": is_webpage,
            "filename": cd_filename,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def url_basename(url: str) -> str:
    try:
        path = urlparse(url).path
        return unquote(path.rsplit("/", 1)[-1])
    except Exception:
        return ""


def filename_matches_url(filename: str, url: str, response_filename: str | None = None) -> bool:
    if not filename or not url:
        return False
    url_name = url_basename(url)
    if url_name == filename:
        return True
    if response_filename and response_filename == filename:
        return True

    def _normalize(name: str) -> str:
        return name.strip().lstrip("_")

    normalized_expected = _normalize(filename)
    if url_name and _normalize(url_name) == normalized_expected:
        return True
    if response_filename and _normalize(response_filename) == normalized_expected:
        return True
    return False
