"""xAI Responses API cache and usage helpers."""

from __future__ import annotations

from typing import Any


PROMPT_CACHE_KEY = "custom-comfy-model-manager-ai-lookup-v1"

# Current grok-4.3 public pricing when this feature was added. This is only an
# estimate for the review UI; billing truth remains xAI usage/cost reporting.
DEFAULT_INPUT_PER_1M = 1.25
DEFAULT_CACHED_INPUT_PER_1M = 0.20
DEFAULT_OUTPUT_PER_1M = 2.50


def extract_usage(payload: dict[str, Any] | None) -> dict[str, int]:
    usage = (payload or {}).get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}

    input_tokens = int(usage.get("input_tokens") or 0)
    cached_input_tokens = int(input_details.get("cached_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)

    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "uncached_input_tokens": max(0, input_tokens - cached_input_tokens),
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": int(usage.get("total_tokens") or (input_tokens + output_tokens)),
    }


def add_usage(total: dict[str, int], usage: dict[str, int]) -> dict[str, int]:
    for key, value in usage.items():
        total[key] = int(total.get(key, 0)) + int(value or 0)
    return total


def estimate_cost(usage: dict[str, int]) -> float:
    uncached_input = usage.get("uncached_input_tokens", 0)
    cached_input = usage.get("cached_input_tokens", 0)
    output = usage.get("output_tokens", 0)
    return (
        (uncached_input / 1_000_000) * DEFAULT_INPUT_PER_1M
        + (cached_input / 1_000_000) * DEFAULT_CACHED_INPUT_PER_1M
        + (output / 1_000_000) * DEFAULT_OUTPUT_PER_1M
    )


def format_usage_summary(usage: dict[str, int]) -> str:
    input_tokens = usage.get("input_tokens", 0)
    cached_input = usage.get("cached_input_tokens", 0)
    uncached_input = usage.get("uncached_input_tokens", 0)
    output = usage.get("output_tokens", 0)
    total = usage.get("total_tokens", input_tokens + output)
    cost = estimate_cost(usage)
    cache_pct = (cached_input / input_tokens * 100) if input_tokens else 0
    return (
        "xAI token usage: "
        f"input {input_tokens:,} ({cached_input:,} cached, {uncached_input:,} uncached, {cache_pct:.0f}% cached), "
        f"output {output:,}, total {total:,}, est. ${cost:.4f} at grok-4.3 rates."
    )
