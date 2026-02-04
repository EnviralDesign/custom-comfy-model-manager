"""Shared helpers for AI source URL lookup and URL validation."""

from __future__ import annotations

import json
from typing import Any, Optional

import requests

from app.services.civitai_api import find_civitai_download


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
        "match is found you may search elsewhere. Try filename permutations (swap underscores/dashes "
        "for spaces, drop fp16/fp8/pruned/full suffixes, trim trailing version tags like v1.2). "
        "Only return a URL if the filename matches exactly (case-sensitive) and points to a direct "
        "file download (not a landing page). "
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


def call_ai_lookup(
    *,
    base_url: str,
    api_key: str,
    model: str,
    filename: str,
    relpath: str | None,
    file_hash: str | None,
    civitai_base_url: str,
    civitai_api_key: str | None,
    huggingface_api_key: str | None,
    lookup_mode: str = "tool_agent",
    tool_max_steps: int = 12,
) -> dict:
    if lookup_mode == "tool_agent":
        if not api_key:
            civitai_result = find_civitai_download(
                filename=filename,
                file_hash=file_hash,
                base_url=civitai_base_url,
                api_key=civitai_api_key,
            )
            steps = normalize_steps(civitai_result.get("steps"))
            steps.append("Tool agent skipped: XAI_API_KEY not configured.")
            return {
                "found": bool(civitai_result.get("found")),
                "url": civitai_result.get("url"),
                "source": civitai_result.get("source"),
                "notes": civitai_result.get("notes"),
                "steps": steps,
            }

        from app.services.ai_tool_agent import run_tool_agent_lookup

        return run_tool_agent_lookup(
            base_url=base_url,
            api_key=api_key,
            model=model,
            filename=filename,
            relpath=relpath,
            file_hash=file_hash,
            civitai_base_url=civitai_base_url,
            civitai_api_key=civitai_api_key,
            huggingface_api_key=huggingface_api_key,
            max_steps=tool_max_steps,
            require_exact_filename=True,
        )

    civitai_result = find_civitai_download(
        filename=filename,
        file_hash=file_hash,
        base_url=civitai_base_url,
        api_key=civitai_api_key,
    )

    if civitai_result.get("found") and civitai_result.get("url"):
        return {
            "found": True,
            "url": civitai_result.get("url"),
            "source": civitai_result.get("source") or "civitai",
            "notes": civitai_result.get("notes"),
            "steps": normalize_steps(civitai_result.get("steps")),
        }

    if not api_key:
        steps = normalize_steps(civitai_result.get("steps"))
        steps.append("xAI lookup skipped: XAI_API_KEY not configured.")
        return {
            "found": False,
            "url": None,
            "source": None,
            "notes": None,
            "steps": steps,
        }

    xai_result = call_xai_lookup(
        base_url=base_url,
        api_key=api_key,
        model=model,
        filename=filename,
        relpath=relpath,
    )

    combined_steps = []
    combined_steps.extend(normalize_steps(civitai_result.get("steps")))
    combined_steps.extend(normalize_steps(xai_result.get("steps")))
    xai_result["steps"] = combined_steps
    return xai_result
