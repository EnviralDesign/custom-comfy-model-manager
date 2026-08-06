"""Validated path resolution for destructive queue operations."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


class UnsafeQueuePath(ValueError):
    """Raised when a queue path can escape its configured models root."""


def normalize_queue_relpath(relpath: str) -> str:
    value = str(relpath or "")
    if not value or "\x00" in value:
        raise UnsafeQueuePath("Queue path must be a non-empty relative path")

    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute() or windows_path.drive or windows_path.root:
        raise UnsafeQueuePath("Absolute queue paths are not allowed")

    normalized = value.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    if posix_path.is_absolute() or any(part == ".." for part in posix_path.parts):
        raise UnsafeQueuePath("Queue path cannot leave the configured models root")

    parts = [part for part in posix_path.parts if part not in ("", ".")]
    if not parts:
        raise UnsafeQueuePath("Queue path must identify a file")
    return "/".join(parts)


def resolve_queue_path(root: Path, relpath: str) -> tuple[Path, str]:
    """Resolve a queue path and prove it remains beneath ``root``.

    ``Path.resolve`` follows existing symlinks and Windows junctions, including
    when only a destination parent exists, so aliases cannot bypass the root.
    """
    normalized = normalize_queue_relpath(relpath)
    root_resolved = Path(root).resolve(strict=False)
    unresolved = root_resolved.joinpath(*normalized.split("/"))
    if unresolved.is_symlink():
        raise UnsafeQueuePath("Queue operations cannot target symbolic links")
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafeQueuePath("Queue path cannot leave the configured models root") from exc
    if candidate == root_resolved:
        raise UnsafeQueuePath("Queue path must identify a file")
    return candidate, normalized
