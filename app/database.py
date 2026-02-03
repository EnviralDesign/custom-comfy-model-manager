"""SQLite database setup and connection management."""

import aiosqlite
from pathlib import Path
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from app.config import get_settings

# SQL schema for Phase 1
SCHEMA = """
-- File index cache: stores discovered files from both sides
CREATE TABLE IF NOT EXISTS file_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    side TEXT NOT NULL CHECK (side IN ('local', 'lake')),
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    hash TEXT,  -- NULL if not yet computed
    hash_computed_at TEXT,  -- ISO timestamp when hash was computed
    indexed_at TEXT NOT NULL,  -- ISO timestamp
    UNIQUE(side, relpath)
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_file_index_side ON file_index(side);
CREATE INDEX IF NOT EXISTS idx_file_index_relpath ON file_index(relpath);
CREATE INDEX IF NOT EXISTS idx_file_index_hash ON file_index(hash) WHERE hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_file_index_size ON file_index(size);

-- Queue: transfer and delete tasks
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL CHECK (task_type IN ('copy', 'delete')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    src_side TEXT,  -- 'local' or 'lake', NULL for delete tasks
    src_relpath TEXT,
    dst_side TEXT,  -- 'local' or 'lake'
    dst_relpath TEXT,
    size_bytes INTEGER,
    bytes_transferred INTEGER DEFAULT 0,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);

-- Dedupe scan results (cached for UI display)
CREATE TABLE IF NOT EXISTS dedupe_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    side TEXT NOT NULL CHECK (side IN ('local', 'lake')),
    hash TEXT NOT NULL,
    scan_id TEXT NOT NULL,  -- UUID to group results from one scan
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dedupe_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES dedupe_groups(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    keep INTEGER DEFAULT 0  -- 1 if user selected to keep this file
);

CREATE INDEX IF NOT EXISTS idx_dedupe_groups_scan ON dedupe_groups(scan_id);
"""


async def init_db(db_path: Path) -> None:
    """Initialize the database with schema."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Get a database connection."""
    settings = get_settings()
    db_path = settings.get_db_path()
    
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def startup_db() -> None:
    """Initialize database on application startup."""
    settings = get_settings()
    db_path = settings.get_db_path()
    await init_db(db_path)
