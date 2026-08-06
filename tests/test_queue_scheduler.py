import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from app.database import init_db
from app.services.downloader import DownloadJob, DownloadManager
from app.services.hasher import HasherService
from app.services.queue_paths import UnsafeQueuePath, resolve_queue_path
from app.services.queue_scheduler import (
    LANE_CLEANUP,
    LANE_INTEGRITY,
    LANE_TRANSFER,
    select_runnable_tasks,
    task_resources,
)
from app.services.worker import QueueWorker

ROOTS = {
    "local": Path("C:/models"),
    "lake": Path("Y:/models"),
}
LIMITS = {LANE_TRANSFER: 2, LANE_CLEANUP: 4}


def task(task_id, task_type, *, src_side=None, src=None, dst_side=None, dst=None):
    return {
        "id": task_id,
        "task_type": task_type,
        "src_side": src_side,
        "src_relpath": src,
        "dst_side": dst_side,
        "dst_relpath": dst,
    }


def active(item, lane):
    return {"lane": lane, "resources": task_resources(item, ROOTS)}


def test_selects_unrelated_transfers_and_cleanup_concurrently():
    pending = [
        task(1, "copy", src_side="local", src="a.bin", dst_side="lake", dst="a.bin"),
        task(2, "copy", src_side="local", src="b.bin", dst_side="lake", dst="b.bin"),
        task(3, "delete", dst_side="local", dst="old/c.bin"),
    ]

    selected = select_runnable_tasks(pending, [], ROOTS, LIMITS)

    assert [item["id"] for item in selected] == [1, 2, 3]


def test_cleanup_can_overtake_full_transfer_lane_when_paths_are_unrelated():
    running_copy = task(
        1, "copy", src_side="local", src="busy.bin", dst_side="lake", dst="busy.bin"
    )
    pending = [
        task(2, "copy", src_side="local", src="next.bin", dst_side="lake", dst="next.bin"),
        task(3, "delete", dst_side="local", dst="unrelated.bin"),
    ]

    selected = select_runnable_tasks(
        pending,
        [active(running_copy, LANE_TRANSFER)],
        ROOTS,
        {LANE_TRANSFER: 1, LANE_CLEANUP: 1},
    )

    assert [item["id"] for item in selected] == [3]


def test_delete_cannot_race_running_copy_source():
    running_copy = task(
        1, "copy", src_side="local", src="same.bin", dst_side="lake", dst="same.bin"
    )
    pending = [task(2, "delete", dst_side="local", dst="SAME.bin")]

    selected = select_runnable_tasks(
        pending,
        [active(running_copy, LANE_TRANSFER)],
        ROOTS,
        LIMITS,
    )

    assert selected == []


def test_later_delete_cannot_overtake_older_pending_copy_of_same_source():
    pending = [
        task(1, "copy", src_side="local", src="same.bin", dst_side="lake", dst="same.bin"),
        task(2, "delete", dst_side="local", dst="same.bin"),
    ]

    selected = select_runnable_tasks(
        pending,
        [],
        ROOTS,
        {LANE_TRANSFER: 0, LANE_CLEANUP: 4},
    )

    assert selected == []


def test_integrity_waits_for_all_normal_pending_and_running_work():
    integrity = task(1, "hash_file", src="model.bin")
    cleanup = task(2, "delete", dst_side="local", dst="old.bin")

    selected = select_runnable_tasks([integrity, cleanup], [], ROOTS, LIMITS)
    assert [item["id"] for item in selected] == [2]

    running_cleanup = active(cleanup, LANE_CLEANUP)
    assert select_runnable_tasks([integrity], [running_cleanup], ROOTS, LIMITS) == []


def test_only_one_integrity_task_runs_and_it_is_globally_exclusive():
    hash_task = task(1, "hash_file", src="model.bin")
    dedupe_task = task(2, "dedupe_scan", src_side="local")

    selected = select_runnable_tasks([hash_task, dedupe_task], [], ROOTS, LIMITS)
    assert [item["id"] for item in selected] == [1]

    running_integrity = {"lane": LANE_INTEGRITY, "resources": frozenset()}
    normal = task(3, "delete", dst_side="local", dst="old.bin")
    assert select_runnable_tasks([normal], [running_integrity], ROOTS, LIMITS) == []


@pytest.mark.asyncio
async def test_claim_is_atomic(monkeypatch, tmp_path):
    db_path = tmp_path / "queue.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE queue (
                id INTEGER PRIMARY KEY,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error_message TEXT
            )
            """
        )
        await db.execute(
            "INSERT INTO queue (id, task_type, status) VALUES (1, 'copy', 'pending')"
        )
        await db.execute(
            "CREATE TABLE download_jobs (id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        )
        await db.commit()

    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(db_path) as db:
            yield db

    monkeypatch.setattr("app.services.worker.get_db", test_db)
    worker = QueueWorker()

    assert await worker._claim_task(1) is True
    assert await worker._claim_task(1) is False


@pytest.mark.asyncio
async def test_integrity_claim_atomically_waits_for_queued_download(monkeypatch, tmp_path):
    db_path = tmp_path / "integrity-gate.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE queue (
                id INTEGER PRIMARY KEY, task_type TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT, completed_at TEXT, error_message TEXT
            )
            """
        )
        await db.execute(
            "CREATE TABLE download_jobs (id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT INTO queue (id, task_type, status) VALUES (1, 'hash_file', 'pending')"
        )
        await db.execute("INSERT INTO download_jobs VALUES (1, 'queued')")
        await db.commit()

    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(db_path) as db:
            yield db

    monkeypatch.setattr("app.services.worker.get_db", test_db)
    worker = QueueWorker()

    assert await worker._claim_task(1) is False
    async with aiosqlite.connect(db_path) as db:
        row = await db.execute_fetchall("SELECT status FROM queue WHERE id = 1")
    assert row == [("pending",)]


def test_cancellation_is_scoped_to_one_running_task():
    worker = QueueWorker()
    worker._active = {
        1: {"cancel_event": asyncio.Event()},
        2: {"cancel_event": asyncio.Event()},
    }

    assert worker.cancel_task(1) is True
    assert worker._active[1]["cancel_event"].is_set() is True
    assert worker._active[2]["cancel_event"].is_set() is False


def test_cancellation_is_rejected_after_irreversible_commit_begins():
    worker = QueueWorker()
    event = asyncio.Event()
    worker._active = {
        1: {"cancel_event": event, "commit_started": True},
    }

    assert worker.request_task_cancellation(1) == "too_late"
    assert event.is_set() is False


def test_cancel_all_reports_irreversible_tasks_separately():
    worker = QueueWorker()
    cancellable = asyncio.Event()
    irreversible = asyncio.Event()
    worker._active = {
        1: {"cancel_event": cancellable, "commit_started": False},
        2: {"cancel_event": irreversible, "commit_started": True},
    }

    assert worker.abort_all_tasks() == (1, 1)
    assert cancellable.is_set() is True
    assert irreversible.is_set() is False


@pytest.mark.asyncio
async def test_accepted_late_integrity_cancellation_cannot_complete(monkeypatch, tmp_path):
    db_path = tmp_path / "late-cancel.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE queue (
                id INTEGER PRIMARY KEY, status TEXT, completed_at TEXT,
                operation_phase TEXT, error_message TEXT,
                retry_count INTEGER DEFAULT 0
            )
            """
        )
        await db.execute("INSERT INTO queue (id, status) VALUES (1, 'running')")
        await db.commit()

    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def capture_broadcast(_event_type, _payload):
        return None

    monkeypatch.setattr("app.services.worker.get_db", test_db)
    monkeypatch.setattr("app.services.worker.broadcast", capture_broadcast)
    worker = QueueWorker()
    worker._active = {
        1: {
            "lane": LANE_INTEGRITY,
            "resources": frozenset(),
            "cancel_event": asyncio.Event(),
            "requeue_on_cancel": False,
            "commit_started": False,
            "task": asyncio.current_task(),
        }
    }

    async def cancel_at_end(_task):
        assert worker.request_task_cancellation(1) == "requested"

    monkeypatch.setattr(worker, "_execute_task_body", cancel_at_end)
    QueueWorker._running = True
    try:
        await worker._run_claimed_task(task(1, "hash_file", src="model.bin"))
    finally:
        QueueWorker._running = False

    async with aiosqlite.connect(db_path) as db:
        row = await db.execute_fetchall("SELECT status FROM queue WHERE id = 1")
    assert row == [("cancelled",)]


@pytest.mark.asyncio
async def test_completion_boundary_rejects_cancellation_before_db_await(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "completion-boundary.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE queue (
                id INTEGER PRIMARY KEY, status TEXT, completed_at TEXT,
                operation_phase TEXT
            )
            """
        )
        await db.execute("INSERT INTO queue (id, status) VALUES (1, 'running')")
        await db.commit()

    entered_db = asyncio.Event()
    release_db = asyncio.Event()

    @asynccontextmanager
    async def gated_db():
        entered_db.set()
        await release_db.wait()
        async with aiosqlite.connect(db_path) as db:
            yield db

    async def capture_broadcast(_event_type, _payload):
        return None

    async def completed_body(_task):
        return None

    monkeypatch.setattr("app.services.worker.get_db", gated_db)
    monkeypatch.setattr("app.services.worker.broadcast", capture_broadcast)
    worker = QueueWorker()
    worker._active = {
        1: {
            "lane": LANE_INTEGRITY,
            "resources": frozenset(),
            "cancel_event": asyncio.Event(),
            "requeue_on_cancel": False,
            "commit_started": False,
        }
    }
    monkeypatch.setattr(worker, "_execute_task_body", completed_body)

    QueueWorker._running = True
    runner = asyncio.create_task(
        worker._run_claimed_task(task(1, "hash_file", src="model.bin"))
    )
    try:
        await entered_db.wait()
        assert worker.request_task_cancellation(1) == "too_late"
        release_db.set()
        await runner
    finally:
        QueueWorker._running = False
        release_db.set()

    async with aiosqlite.connect(db_path) as db:
        row = await db.execute_fetchall("SELECT status FROM queue WHERE id = 1")
    assert row == [("completed",)]


def test_download_start_atomically_waits_for_running_integrity(monkeypatch, tmp_path):
    db_path = tmp_path / "download-gate.db"
    asyncio.run(init_db(db_path))
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO queue (task_type, status, created_at)
            VALUES ('hash_file', 'running', 'now')
            """
        )
        db.commit()

    settings = SimpleNamespace(get_db_path=lambda: db_path)
    monkeypatch.setattr("app.services.downloader.get_settings", lambda: settings)

    class NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("app.services.downloader.threading.Thread", NoopThread)
    manager = object.__new__(DownloadManager)
    manager._active = set()
    job = DownloadJob(id=1, url="https://example.com/model.bin")

    assert manager._start_job_locked(job) is False
    assert job.status == "queued"

    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE queue SET status = 'completed'")
        db.commit()

    assert manager._start_job_locked(job) is True
    assert job.status == "running"
    assert manager._active == {1}


@pytest.mark.parametrize(
    "relpath",
    [
        "../outside.bin",
        "folder/../../outside.bin",
        "C:\\outside.bin",
        "\\\\server\\share\\outside.bin",
        "/absolute/outside.bin",
        "",
    ],
)
def test_queue_paths_reject_root_escape(tmp_path, relpath):
    with pytest.raises(UnsafeQueuePath):
        resolve_queue_path(tmp_path, relpath)


def test_queue_paths_normalize_safe_relative_paths(tmp_path):
    resolved, normalized = resolve_queue_path(tmp_path, "folder\\nested/./model.bin")

    assert resolved == tmp_path / "folder" / "nested" / "model.bin"
    assert normalized == "folder/nested/model.bin"


def test_queue_paths_reject_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable")

    with pytest.raises(UnsafeQueuePath):
        resolve_queue_path(root, "escape/model.bin")


def test_queue_paths_reject_final_symlink_even_when_target_is_inside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.bin"
    target.write_bytes(b"target")
    link = root / "link.bin"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is unavailable")

    with pytest.raises(UnsafeQueuePath):
        resolve_queue_path(root, "link.bin")


@pytest.mark.asyncio
async def test_integrity_idle_window_waits_for_downloads(monkeypatch):
    worker = QueueWorker()
    worker.settings = SimpleNamespace(queue_integrity_idle_seconds=0)
    integrity = task(1, "hash_file", src="model.bin")

    async def downloads_busy():
        return True

    monkeypatch.setattr(worker, "_has_active_downloads", downloads_busy)
    assert await worker._apply_integrity_idle_policy([integrity], [integrity]) == []
    assert worker._integrity_idle_since is None


@pytest.mark.asyncio
async def test_running_integrity_is_preempted_when_normal_work_arrives(monkeypatch):
    worker = QueueWorker()
    worker.settings = SimpleNamespace(queue_integrity_idle_seconds=0)
    cancel_event = asyncio.Event()
    worker._active = {
        1: {
            "lane": LANE_INTEGRITY,
            "resources": frozenset(),
            "cancel_event": cancel_event,
            "requeue_on_cancel": False,
        }
    }
    cleanup = task(2, "delete", dst_side="local", dst="old.bin")

    async def downloads_idle():
        return False

    monkeypatch.setattr(worker, "_has_active_downloads", downloads_idle)

    selected = await worker._apply_integrity_idle_policy([cleanup], [])

    assert selected == []
    assert cancel_event.is_set() is True
    assert worker._active[1]["requeue_on_cancel"] is True


@pytest.mark.asyncio
async def test_preempted_integrity_returns_to_pending(monkeypatch, tmp_path):
    db_path = tmp_path / "preempt.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE queue (
                id INTEGER PRIMARY KEY, status TEXT, started_at TEXT,
                completed_at TEXT, bytes_transferred INTEGER, error_message TEXT,
                retry_count INTEGER DEFAULT 0
            )
            """
        )
        await db.execute(
            "INSERT INTO queue VALUES (1, 'running', 'now', NULL, 50, NULL, 0)"
        )
        await db.commit()

    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    events = []

    async def capture_broadcast(event_type, payload):
        events.append((event_type, payload))

    async def cancelled_body(_task):
        raise asyncio.CancelledError("preempted")

    monkeypatch.setattr("app.services.worker.get_db", test_db)
    monkeypatch.setattr("app.services.worker.broadcast", capture_broadcast)

    worker = QueueWorker()
    running = asyncio.current_task()
    worker._active = {
        1: {
            "lane": LANE_INTEGRITY,
            "resources": frozenset(),
            "cancel_event": asyncio.Event(),
            "requeue_on_cancel": True,
            "task": running,
        }
    }
    monkeypatch.setattr(worker, "_execute_task_body", cancelled_body)
    QueueWorker._running = True
    integrity = task(1, "hash_file", src="model.bin")
    try:
        await worker._run_claimed_task(integrity)
    finally:
        QueueWorker._running = False

    async with aiosqlite.connect(db_path) as db:
        row = await db.execute_fetchall(
            "SELECT status, started_at, bytes_transferred FROM queue WHERE id = 1"
        )
    assert row == [("pending", None, 0)]
    assert any(event_type == "task_deferred" for event_type, _ in events)


@pytest.mark.asyncio
async def test_pending_hasher_processes_every_database_row(monkeypatch, tmp_path):
    db_path = tmp_path / "hash.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE file_index (side TEXT, relpath TEXT, hash TEXT, size INTEGER)"
        )
        await db.executemany(
            "INSERT INTO file_index (side, relpath, hash, size) VALUES ('local', ?, NULL, 10)",
            [("a.bin",), ("b.bin",)],
        )
        await db.commit()

    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    processed = []

    async def fake_get_hash(side, relpath, **kwargs):
        processed.append((side, relpath))
        return "hash"

    monkeypatch.setattr("app.services.hasher.get_db", test_db)
    hasher = HasherService()
    monkeypatch.setattr(hasher, "get_hash", fake_get_hash)

    count = await hasher.hash_all_pending("local")

    assert count == 2
    assert processed == [("local", "a.bin"), ("local", "b.bin")]


@pytest.mark.asyncio
async def test_cancelled_copy_keeps_existing_destination_and_removes_staging(
    tmp_path,
):
    local_root = tmp_path / "local"
    lake_root = tmp_path / "lake"
    local_root.mkdir()
    lake_root.mkdir()
    (local_root / "model.bin").write_bytes(b"new model")
    destination = lake_root / "model.bin"
    destination.write_bytes(b"existing model")

    worker = QueueWorker()
    worker.settings = SimpleNamespace(
        local_models_root=local_root,
        lake_models_root=lake_root,
    )
    cancel_event = asyncio.Event()
    cancel_event.set()
    worker._active = {1: {"cancel_event": cancel_event}}
    QueueWorker._running = True

    copy_task = task(
        1,
        "copy",
        src_side="local",
        src="model.bin",
        dst_side="lake",
        dst="model.bin",
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await worker._execute_copy(copy_task)
    finally:
        QueueWorker._running = False

    assert destination.read_bytes() == b"existing model"
    assert list(lake_root.glob(".*.part")) == []


@pytest.mark.asyncio
async def test_successful_copy_atomically_replaces_destination_and_updates_index(
    monkeypatch, tmp_path
):
    local_root = tmp_path / "local"
    lake_root = tmp_path / "lake"
    local_root.mkdir()
    lake_root.mkdir()
    source = local_root / "model.bin"
    source.write_bytes(b"new model contents")
    destination = lake_root / "model.bin"
    destination.write_bytes(b"old")

    db_path = tmp_path / "copy.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE queue (id INTEGER PRIMARY KEY, status TEXT, bytes_transferred INTEGER, operation_phase TEXT)"
        )
        await db.execute("INSERT INTO queue VALUES (1, 'running', 0, NULL)")
        await db.execute(
            """
            CREATE TABLE file_index (
                side TEXT, relpath TEXT, size INTEGER, mtime_ns INTEGER,
                hash TEXT, hash_computed_at TEXT, indexed_at TEXT,
                UNIQUE(side, relpath)
            )
            """
        )
        stat = source.stat()
        await db.execute(
            "INSERT INTO file_index VALUES ('local', 'model.bin', ?, ?, NULL, NULL, 'now')",
            (stat.st_size, stat.st_mtime_ns),
        )
        await db.commit()

    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def no_broadcast(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.worker.get_db", test_db)
    monkeypatch.setattr("app.services.worker.broadcast", no_broadcast)

    worker = QueueWorker()
    worker.settings = SimpleNamespace(
        local_models_root=local_root,
        lake_models_root=lake_root,
    )
    worker._active = {1: {"cancel_event": asyncio.Event()}}
    QueueWorker._running = True
    copy_task = task(
        1,
        "copy",
        src_side="local",
        src="model.bin",
        dst_side="lake",
        dst="model.bin",
    )
    try:
        await worker._execute_copy(copy_task)
    finally:
        QueueWorker._running = False

    assert destination.read_bytes() == source.read_bytes()
    assert list(lake_root.glob(".*.part")) == []
    async with aiosqlite.connect(db_path) as db:
        rows = await db.execute_fetchall(
            "SELECT side, hash FROM file_index WHERE relpath = 'model.bin' ORDER BY side"
        )
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1]


@pytest.mark.asyncio
async def test_move_recovers_after_rename_completed_before_restart(monkeypatch, tmp_path):
    local_root = tmp_path / "local"
    lake_root = tmp_path / "lake"
    local_root.mkdir()
    lake_root.mkdir()
    destination = local_root / "renamed.bin"
    destination.write_bytes(b"already moved")

    db_path = tmp_path / "move.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE file_index (
                side TEXT, relpath TEXT, size INTEGER, mtime_ns INTEGER,
                hash TEXT, hash_computed_at TEXT, indexed_at TEXT,
                UNIQUE(side, relpath)
            )
            """
        )
        stat = destination.stat()
        await db.execute(
            "INSERT INTO file_index VALUES ('local', 'original.bin', ?, ?, 'abc', 'then', 'then')",
            (stat.st_size, stat.st_mtime_ns),
        )
        await db.execute(
            "CREATE TABLE source_urls (key TEXT PRIMARY KEY, relpath TEXT)"
        )
        await db.execute(
            "INSERT INTO source_urls VALUES ('relpath:original.bin', 'original.bin')"
        )
        await db.commit()

    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    monkeypatch.setattr("app.services.worker.get_db", test_db)
    worker = QueueWorker()
    worker.settings = SimpleNamespace(
        local_models_root=local_root,
        lake_models_root=lake_root,
    )
    worker._active = {
        1: {
            "cancel_event": asyncio.Event(),
            "commit_started": False,
        }
    }
    QueueWorker._running = True
    move_task = task(
        1,
        "move",
        src_side="local",
        src="original.bin",
        dst_side="local",
        dst="renamed.bin",
    )
    move_task["operation_phase"] = "committing"
    try:
        await worker._execute_move(move_task)
    finally:
        QueueWorker._running = False

    async with aiosqlite.connect(db_path) as db:
        index_rows = await db.execute_fetchall(
            "SELECT relpath, hash FROM file_index"
        )
        source_rows = await db.execute_fetchall(
            "SELECT key, relpath FROM source_urls"
        )
    assert index_rows == [("renamed.bin", "abc")]
    assert source_rows == [("relpath:renamed.bin", "renamed.bin")]
