"""Shared helpers for AI source URL lookup and URL validation."""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import requests


def check_url_sync(url: str) -> dict:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.head(url, allow_redirects=True, timeout=10, headers=headers)

        # If 404 or other error, or if Content-Length is missing (some sites block HEAD), try GET
        if response.status_code != 200 or not response.headers.get("Content-Length"):
            response = requests.get(url, stream=True, timeout=10, headers=headers)

        size = response.headers.get("Content-Length")
        content_type = response.headers.get("Content-Type", "").lower()

        # Heuristic: if it's text/html, it's likely a landing page, not a direct download
        is_webpage = "text/html" in content_type

        return {
            "ok": response.status_code == 200 and not is_webpage,
            "status": response.status_code,
            "size": int(size) if size else None,
            "type": content_type,
            "url": response.url,
            "is_webpage": is_webpage,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def extract_response_text(payload: dict) -> str:
    if not payload:
        return ""
    if isinstance(payload.get("output_text"), str):
        return payload.get("output_text", "")

    output = payload.get("output", [])
    if isinstance(output, list):
        text_parts = []
        for item in output:
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                part_type = part.get("type")
                if part_type in ("output_text", "text"):
                    text_parts.append(part.get("text", ""))
        if text_parts:
            return "\n".join(text_parts)

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            return message.get("content", "")

    return ""


def extract_json_object(text: str) -> Optional[dict]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = cleaned[start:end + 1]
            try:
                return json.loads(snippet)
            except Exception:
                return None
    return None


def url_basename(url: str) -> str:
    try:
        path = urlparse(url).path
        return unquote(path.rsplit("/", 1)[-1])
    except Exception:
        return ""


def filename_matches_url(filename: str, url: str) -> bool:
    if not filename or not url:
        return False
    return url_basename(url) == filename


def normalize_steps(raw_steps: Any) -> list[str]:
    if not raw_steps:
        return []
    if isinstance(raw_steps, list):
        normalized: list[str] = []
        for step in raw_steps:
            if isinstance(step, str):
                normalized.append(step.strip())
            elif isinstance(step, dict):
                message = step.get("message") or step.get("step") or step.get("text")
                if isinstance(message, str) and message.strip():
                    normalized.append(message.strip())
        return [s for s in normalized if s]
    if isinstance(raw_steps, str):
        return [raw_steps.strip()] if raw_steps.strip() else []
    return []


def call_xai_lookup(
    *,
    base_url: str,
    api_key: str,
    model: str,
    filename: str,
    relpath: str | None,
) -> dict:
    system_prompt = (
        "You are a web research assistant. Your task is to find a public direct download URL "
        "for the exact file name provided. Start with Hugging Face and Civitai, but if no exact "
        "match is found you may search elsewhere. Only return a URL if the filename matches "
        "exactly (case-sensitive) and points to a direct file download (not a landing page). "
        "If you cannot find an exact match, return found=false and url=null. "
        "Return ONLY a JSON object with keys: found (boolean), url (string|null), source (string|null), "
        "notes (string|null), steps (array of short strings)."
    )
    user_prompt = f"File name: {filename}\nPath: {relpath or ''}"

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "tools": [{"type": "web_search"}],
        "temperature": 0.2,
        "max_output_tokens": 600,
        "store": False,
    }

    response = requests.post(
        f"{base_url.rstrip('/')}/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )

    if response.status_code >= 400:
        return {
            "error": f"xAI error {response.status_code}: {response.text}",
        }

    data = response.json()
    text = extract_response_text(data)
    result = extract_json_object(text) or {}
    return {
        "found": bool(result.get("found")),
        "url": (result.get("url") or "").strip(),
        "source": (result.get("source") or "").strip(),
        "notes": (result.get("notes") or "").strip(),
        "steps": normalize_steps(result.get("steps")),
        "raw_text": text,
    }
