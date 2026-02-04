"""Tool-calling agent for model URL discovery (Civitai + Hugging Face)."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import requests

from app.services.civitai_api import parse_filename_hints
from app.services.url_utils import check_url_sync, filename_matches_url
from app.services.agent_tools import (
    civitai_by_hash,
    civitai_model_version,
    civitai_search,
    hf_search,
    hf_model_info,
    hf_resolve,
    url_validate,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _extract_response_text(payload: dict) -> str:
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


def _extract_json_object(text: str) -> dict | None:
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


def _call_xai_agent_step(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_output_tokens: int = 400,
) -> dict:
    payload = {
        "model": model,
        "input": messages,
        "temperature": 0.2,
        "max_output_tokens": max_output_tokens,
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
        return {"error": f"xAI error {response.status_code}: {response.text}"}
    data = response.json()
    text = _extract_response_text(data)
    return {"text": text}



def run_tool_agent_lookup(
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
    max_steps: int = 12,
    require_exact_filename: bool = True,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict:
    steps: list[str] = []
    hints = parse_filename_hints(filename) if filename else {}

    tool_list = [
        {"name": "civitai.by_hash", "args": {"hash": "string"}},
        {"name": "civitai.search", "args": {"query": "string", "limit": "int", "page": "int|null", "cursor": "string|null", "types": "string|null", "supportsGeneration": "bool|null", "primaryFileOnly": "bool|null", "nsfw": "bool|null", "tag": "string|null"}},
        {"name": "civitai.model_version", "args": {"id": "int"}},
        {"name": "hf.search", "args": {"query": "string", "limit": "int"}},
        {"name": "hf.model_info", "args": {"repo_id": "string"}},
        {"name": "hf.resolve", "args": {"repo_id": "string", "file": "string", "revision": "string|null", "validate": "bool"}},
        {"name": "url.validate", "args": {"url": "string"}},
    ]

    system_prompt = (
        "You are a tool-using agent that must find a direct download URL for a model file.\n"
        "Rules:\n"
        "- Use ONLY the tools provided; do not guess URLs.\n"
        "- Always respond with a single JSON object and nothing else.\n"
        "- Prefer exact filename matches. If require_exact_filename is true, only return a URL if the "
        "downloaded filename matches exactly (case-sensitive).\n"
        "- Use url.validate before finalizing a URL unless a tool already returned validation.\n"
        "- If you cannot find a match, respond with action=final and found=false.\n\n"
        "Search strategy (derive terms yourself):\n"
        "- Break the filename into high-impact tokens: split underscores/dashes, camelCase, numbers, and version tags.\n"
        "- Try short, meaningful query combos (2-4 tokens). Include model name fragments, family names, and version cues.\n"
        "- Drop file extensions and low-signal tokens (e.g., 'safetensors', 'ckpt', 'fp16', 'pruned') from search.\n"
        "- Try permutations: e.g., 'wan remix', 'wan 2.2', 't2v i2v', 'i2v high', 'v2.0'.\n"
        "- If you have a type hint (e.g., LORA or Checkpoint), pass it via civitai.search types to reduce noise.\n"
        "- You may use supportsGeneration=true to suppress workflows and non-model content, or leave unset for broad search.\n"
        "- Prefer Civitai search first. Run at least 4 distinct civitai.search queries before any hf.search.\n"
        "- Only use hf.search if Civitai searches are exhausted or clearly irrelevant.\n\n"
        "Output schema:\n"
        '{"action":"tool","tool":"name","args":{...}} or '
        '{"action":"final","found":true|false,"url":"string|null","reason":"string"}\n\n'
        "Available tools:\n"
        + "\n".join([f"- {t['name']} {t['args']}" for t in tool_list])
    )

    type_hint = None
    supports_generation_hint: bool | None = None
    if relpath:
        rel = relpath.replace("\\", "/").lower()
        if "/loras/" in rel or rel.endswith("/loras"):
            type_hint = "LORA"
        elif "/checkpoints/" in rel or "/models/" in rel and "/checkpoints/" in rel:
            type_hint = "Checkpoint"
        elif "/embeddings/" in rel:
            type_hint = "TextualInversion"
        elif "/controlnet/" in rel:
            type_hint = "Controlnet"
        elif "/hypernetworks/" in rel:
            type_hint = "Hypernetwork"
        elif "/poses/" in rel:
            type_hint = "Poses"
        elif "/aesthetic/" in rel:
            type_hint = "AestheticGradient"

    user_prompt = (
        f"filename={filename}\n"
        f"relpath={relpath or ''}\n"
        f"file_hash={file_hash or ''}\n"
        f"type_hint={type_hint or ''}\n"
        f"require_exact_filename={str(require_exact_filename).lower()}\n"
        f"hints={json.dumps(hints)}"
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    def emit(event_type: str, payload: dict[str, Any]):
        entry = {"time": _now_iso(), "type": event_type, **payload}
        if trace_callback:
            trace_callback(entry)

    tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "civitai.by_hash": lambda args: civitai_by_hash(
            file_hash=args.get("hash") or "",
            base_url=civitai_base_url,
            api_key=civitai_api_key,
        ),
        "civitai.search": lambda args: civitai_search(
            query=args.get("query") or "",
            limit=args.get("limit") or 6,
            page=args.get("page"),
            cursor=args.get("cursor"),
            types=args.get("types") or type_hint,
            supports_generation=args.get("supportsGeneration"),
            primary_file_only=args.get("primaryFileOnly"),
            nsfw=args.get("nsfw"),
            tag=args.get("tag"),
            base_url=civitai_base_url,
            api_key=civitai_api_key,
        ),
        "civitai.model_version": lambda args: civitai_model_version(
            version_id=int(args.get("id")),
            base_url=civitai_base_url,
            api_key=civitai_api_key,
        ),
        "hf.search": lambda args: hf_search(
            query=args.get("query") or "",
            limit=args.get("limit") or 6,
            api_key=huggingface_api_key,
        ),
        "hf.model_info": lambda args: hf_model_info(
            repo_id=args.get("repo_id") or "",
            api_key=huggingface_api_key,
        ),
        "hf.resolve": lambda args: hf_resolve(
            repo_id=args.get("repo_id") or "",
            file_name=args.get("file") or "",
            revision=args.get("revision"),
            validate=bool(args.get("validate", True)),
            api_key=huggingface_api_key,
        ),
        "url.validate": lambda args: url_validate(url=args.get("url") or ""),
    }

    for step in range(max(1, max_steps)):
        if should_cancel and should_cancel():
            return {
                "found": False,
                "url": None,
                "source": None,
                "notes": "Cancelled",
                "steps": steps,
            }

        emit("agent_step", {"step": step + 1})
        response = _call_xai_agent_step(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
        )

        if response.get("error"):
            steps.append(response["error"])
            emit("error", {"message": response["error"]})
            return {
                "found": False,
                "url": None,
                "source": None,
                "notes": response["error"],
                "steps": steps,
            }

        text = response.get("text") or ""
        emit("agent_output", {"text": text})
        action = _extract_json_object(text)
        if not action:
            steps.append("Agent returned invalid JSON.")
            emit("error", {"message": "invalid_json"})
            return {
                "found": False,
                "url": None,
                "source": None,
                "notes": "Agent returned invalid JSON.",
                "steps": steps,
            }

        if action.get("action") == "tool":
            tool_name = action.get("tool")
            raw_args = action.get("args")
            if isinstance(raw_args, dict):
                args = raw_args
            elif raw_args is None:
                # Allow shorthand: args embedded at top-level
                args = {
                    key: value
                    for key, value in action.items()
                    if key not in ("action", "tool", "args")
                }
            else:
                args = {}
            tool_fn = tools.get(tool_name)
            if not tool_fn:
                steps.append(f"Unknown tool: {tool_name}")
                emit("error", {"message": f"unknown_tool:{tool_name}"})
                return {
                    "found": False,
                    "url": None,
                    "source": None,
                    "notes": f"Unknown tool: {tool_name}",
                    "steps": steps,
                }
            emit("tool_call", {"tool": tool_name, "args": args})
            result = tool_fn(args)
            emit("tool_result", {"tool": tool_name, "result": result})
            messages.append({"role": "assistant", "content": json.dumps(action)})
            messages.append({"role": "user", "content": f"TOOL_RESULT {tool_name} {json.dumps(result)}"})
            steps.append(f"Tool {tool_name} executed.")
            continue

        if action.get("action") == "final":
            found = bool(action.get("found"))
            url = (action.get("url") or "").strip()
            reason = action.get("reason") or None
            if not found or not url:
                steps.append(reason or "No match found.")
                return {
                    "found": False,
                    "url": None,
                    "source": None,
                    "notes": reason,
                    "steps": steps,
                }

            validation = check_url_sync(url)
            emit("tool_result", {"tool": "url.validate", "result": validation})
            if not validation.get("ok"):
                steps.append("URL validation failed.")
                messages.append({"role": "assistant", "content": json.dumps(action)})
                messages.append({"role": "user", "content": f"TOOL_RESULT url.validate {json.dumps(validation)}"})
                continue

            if filename and require_exact_filename:
                if not filename_matches_url(filename, url, validation.get("filename")):
                    steps.append("Filename mismatch on validated URL.")
                    messages.append({"role": "assistant", "content": json.dumps(action)})
                    messages.append({"role": "user", "content": f"TOOL_RESULT url.validate {json.dumps(validation)}"})
                    continue

            steps.append("Final URL accepted.")
            return {
                "found": True,
                "url": url,
                "source": action.get("source") or "tool_agent",
                "notes": reason,
                "steps": steps,
            }

        steps.append("Agent response missing action.")
        emit("error", {"message": "missing_action"})
        return {
            "found": False,
            "url": None,
            "source": None,
            "notes": "Agent response missing action.",
            "steps": steps,
        }

    steps.append("Max steps reached without result.")
    return {
        "found": False,
        "url": None,
        "source": None,
        "notes": "Max steps reached without result.",
        "steps": steps,
    }
