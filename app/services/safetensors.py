"""Safetensors header parsing utilities."""

from __future__ import annotations

import json
import struct
from pathlib import Path


class SafetensorsHeaderError(ValueError):
    """Raised when a safetensors header cannot be read or parsed."""


def read_safetensors_header(path: Path, max_header_bytes: int = 8 * 1024 * 1024) -> dict:
    """
    Read the JSON header from a safetensors file without loading tensor data.

    File format:
    - 8 bytes: little-endian unsigned 64-bit header length
    - N bytes: JSON header
    - Remaining bytes: tensor data
    """
    with path.open("rb") as f:
        header_len_bytes = f.read(8)
        if len(header_len_bytes) != 8:
            raise SafetensorsHeaderError("File too short to contain a header length.")

        (header_len,) = struct.unpack("<Q", header_len_bytes)
        if header_len <= 0:
            raise SafetensorsHeaderError("Header length is invalid.")
        if header_len > max_header_bytes:
            raise SafetensorsHeaderError("Header is larger than the allowed limit.")

        header_bytes = f.read(header_len)
        if len(header_bytes) != header_len:
            raise SafetensorsHeaderError("Header appears truncated.")

    try:
        return json.loads(header_bytes.decode("utf-8"))
    except Exception as exc:
        raise SafetensorsHeaderError("Header JSON is invalid.") from exc
