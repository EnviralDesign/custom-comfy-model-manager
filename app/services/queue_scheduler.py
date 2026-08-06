"""Pure scheduling rules for the concurrent file-operation queue."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

from app.services.queue_paths import UnsafeQueuePath, resolve_queue_path

TRANSFER_TYPES = frozenset({"copy", "move"})
CLEANUP_TYPES = frozenset({"delete"})
INTEGRITY_TYPES = frozenset({"verify", "hash_file", "dedupe_scan"})

LANE_TRANSFER = "transfer"
LANE_CLEANUP = "cleanup"
LANE_INTEGRITY = "integrity"


def task_lane(task: Mapping) -> str:
    task_type = task.get("task_type")
    if task_type in TRANSFER_TYPES:
        return LANE_TRANSFER
    if task_type in CLEANUP_TYPES:
        return LANE_CLEANUP
    # Unknown operations take the safest path: idle-only, one at a time.
    return LANE_INTEGRITY


def _canonical_path(root: Path, relpath: str) -> str:
    try:
        resolved, _ = resolve_queue_path(root, relpath)
        return str(resolved).casefold()
    except UnsafeQueuePath:
        # Legacy invalid rows are still claimable so execution can fail them
        # instead of blocking every valid task behind the scheduler tick.
        return f"unsafe:{str(root).casefold()}:{str(relpath).casefold()}"


def task_resources(task: Mapping, roots: Mapping[str, Path]) -> frozenset[str]:
    """Return every concrete file path touched by a non-integrity task."""
    resources: set[str] = set()
    for side_key, relpath_key in (
        ("src_side", "src_relpath"),
        ("dst_side", "dst_relpath"),
    ):
        side = task.get(side_key)
        relpath = task.get(relpath_key)
        root = roots.get(side) if side else None
        if root is not None and relpath:
            resources.add(_canonical_path(root, str(relpath)))
    return frozenset(resources)


def resources_conflict(left: Iterable[str], right: Iterable[str]) -> bool:
    return not set(left).isdisjoint(right)


def select_runnable_tasks(
    pending: list[Mapping],
    active: list[Mapping],
    roots: Mapping[str, Path],
    lane_limits: Mapping[str, int],
) -> list[Mapping]:
    """Select the maximal safe set of tasks to claim during this scheduler tick.

    Pending tasks must be supplied oldest-first. Non-integrity work can overtake
    unrelated older work, but never an older task touching the same path.
    Integrity work is globally exclusive and starts only when no normal work is
    pending or running.
    """
    active_lanes = [str(item["lane"]) for item in active]
    if LANE_INTEGRITY in active_lanes:
        return []

    normal_pending = [task for task in pending if task_lane(task) != LANE_INTEGRITY]
    normal_active = [item for item in active if item["lane"] != LANE_INTEGRITY]

    if not normal_pending and not normal_active:
        for task in pending:
            if task_lane(task) == LANE_INTEGRITY:
                return [task]
        return []

    active_resources: set[str] = set()
    active_counts = {LANE_TRANSFER: 0, LANE_CLEANUP: 0}
    for item in normal_active:
        lane = str(item["lane"])
        active_counts[lane] = active_counts.get(lane, 0) + 1
        active_resources.update(item.get("resources", ()))

    selected: list[Mapping] = []
    older_pending_resources: set[str] = set()

    for task in pending:
        lane = task_lane(task)
        if lane == LANE_INTEGRITY:
            continue

        resources = task_resources(task, roots)
        limit = max(0, int(lane_limits.get(lane, 0)))
        has_capacity = active_counts.get(lane, 0) < limit
        conflicts_active = resources_conflict(resources, active_resources)
        conflicts_older = resources_conflict(resources, older_pending_resources)

        if has_capacity and not conflicts_active and not conflicts_older:
            selected.append(task)
            active_counts[lane] = active_counts.get(lane, 0) + 1
            active_resources.update(resources)

        # Preserve per-path FIFO whether this older task was selected or blocked.
        older_pending_resources.update(resources)

    return selected
