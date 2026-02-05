"""
Utilities for file streaming with Range support.
"""

import os
from pathlib import Path
from typing import BinaryIO, Generator

from fastapi import HTTPException, status, Request
from fastapi.responses import StreamingResponse


DEFAULT_STREAM_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def send_bytes_range_requests(
    file_obj: BinaryIO, start: int, end: int, chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE
) -> Generator[bytes, None, None]:
    """Yield chunks of bytes from file_obj between start and end."""
    file_obj.seek(start)
    bytes_remaining = end - start + 1
    while bytes_remaining > 0:
        chunk = file_obj.read(min(chunk_size, bytes_remaining))
        if not chunk:
            break
        bytes_remaining -= len(chunk)
        yield chunk


def range_requests_response(
    request: Request, file_path: Path, content_type: str = "application/octet-stream"
):
    """
    Returns a StreamingResponse that supports Range headers.
    """
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")

    if not range_header:
        # No range, stream full file in fixed-size binary chunks.
        # Do not iterate file object directly (line-based iteration is slow for binary payloads).
        def iterfile():
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(DEFAULT_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        
        return StreamingResponse(
            iterfile(),
            media_type=content_type,
            headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"}
        )

    # Parse Range: bytes=0-1023
    try:
        unit, ranges = range_header.split("=")
        if unit != "bytes":
             raise ValueError("Only bytes supported")
        
        start_str, end_str = ranges.split("-")
        start = int(start_str)
        if end_str:
            end = int(end_str)
        else:
            end = file_size - 1
            
        if start >= file_size:
             # 416 Range Not Satisfiable
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail="Requested range not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"}
            )
            
        end = min(end, file_size - 1)
        content_length = end - start + 1
        
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Range header")

    def iter_range():
        with open(file_path, "rb") as f:
            yield from send_bytes_range_requests(f, start, end)

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
    }

    return StreamingResponse(
        iter_range(),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=content_type,
        headers=headers,
    )
