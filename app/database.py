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
    task_type TEXT NOT NULL CHECK (task_type IN ('copy', 'move', 'delete', 'verify', 'dedupe_scan', 'hash_file')),
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
    completed_at TEXT,
    verify_folder TEXT
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

-- Source URLs: maps file hashes to public download URLs
CREATE TABLE IF NOT EXISTS source_urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,  -- hash or 'relpath:xxx' for unhashed files
    url TEXT NOT NULL,
    filename_hint TEXT,
    notes TEXT,
    relpath TEXT,  -- set for relpath-based entries (unhashed files)
    added_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_urls_key ON source_urls(key);
CREATE INDEX IF NOT EXISTS idx_source_urls_relpath ON source_urls(relpath) WHERE relpath IS NOT NULL;

-- Safetensors classification cache
CREATE TABLE IF NOT EXISTS safetensors_cache (
    key TEXT PRIMARY KEY,  -- side:relpath
    side TEXT NOT NULL CHECK (side IN ('local', 'lake')),
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_safetensors_cache_relpath ON safetensors_cache(relpath);

-- Downloader jobs (persistent)
CREATE TABLE IF NOT EXISTS download_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    filename TEXT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    bytes_downloaded INTEGER DEFAULT 0,
    total_bytes INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_message TEXT,
    attempts INTEGER DEFAULT 0,
    dest_path TEXT,
    temp_path TEXT,
    target_root TEXT,
    record_source INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_download_jobs_status ON download_jobs(status);

-- AI lookup jobs: background Grok web search and review workflow
CREATE TABLE IF NOT EXISTS ai_lookup_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    decision TEXT CHECK (decision IN ('approved', 'rejected')),
    filename TEXT NOT NULL,
    relpath TEXT,
    file_hash TEXT,
    model TEXT,
    found INTEGER DEFAULT 0,
    accepted INTEGER DEFAULT 0,
    candidate_url TEXT,
    candidate_source TEXT,
    candidate_notes TEXT,
    validation_json TEXT,
    steps_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    decision_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_lookup_status ON ai_lookup_jobs(status);
CREATE INDEX IF NOT EXISTS idx_ai_lookup_decision ON ai_lookup_jobs(decision);
CREATE INDEX IF NOT EXISTS idx_ai_lookup_relpath ON ai_lookup_jobs(relpath);

-- Bundles: named collections of model files
CREATE TABLE IF NOT EXISTS bundles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Bundle assets: links bundles to files
CREATE TABLE IF NOT EXISTS bundle_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    hash TEXT,  -- optional, for verification
    source_url_override TEXT,  -- optional, override global source_url
    UNIQUE(bundle_id, relpath)
);

CREATE INDEX IF NOT EXISTS idx_bundle_assets_bundle ON bundle_assets(bundle_id);
CREATE INDEX IF NOT EXISTS idx_bundle_assets_relpath ON bundle_assets(relpath);
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
    
    # Run migration if needed - check if we can insert 'move'
    async with aiosqlite.connect(db_path) as db:
        # Check if queue table exists
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='queue'")
        if await cursor.fetchone():
            try:
                # Try to insert a dummy move task within a transaction that we roll back
                await db.execute("BEGIN TRANSACTION")
                await db.execute("INSERT INTO queue (task_type, created_at) VALUES ('move', '2000-01-01')")
                await db.execute("ROLLBACK")
            except Exception:
                # Constraint failed, we need to migrate
                print("Migrating queue table to support 'move' tasks...")
                await db.execute("ROLLBACK")
                
                # Rename old table
                await db.execute("DROP TABLE IF EXISTS queue_old")
                await db.execute("ALTER TABLE queue RENAME TO queue_old")
                
                # Create new table with updated constraint
                await db.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL CHECK (task_type IN ('copy', 'move', 'delete', 'verify', 'dedupe_scan', 'hash_file')),
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
                    src_side TEXT,
                    src_relpath TEXT,
                    dst_side TEXT,
                    dst_relpath TEXT,
                    size_bytes INTEGER,
                    bytes_transferred INTEGER DEFAULT 0,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    verify_folder TEXT
                );
                """)
                
                # Copy data back
                # Check column info to see if we have verify_folder in old table
                cursor_cls = await db.execute("PRAGMA table_info(queue_old)")
                cols = [row[1] for row in await cursor_cls.fetchall()]
                has_verify_folder = 'verify_folder' in cols
                
                if has_verify_folder:
                     await db.execute("""
                    INSERT INTO queue (id, task_type, status, src_side, src_relpath, dst_side, dst_relpath, 
                                     size_bytes, bytes_transferred, error_message, retry_count, created_at, started_at, completed_at, verify_folder)
                    SELECT id, task_type, status, src_side, src_relpath, dst_side, dst_relpath, 
                           size_bytes, bytes_transferred, error_message, retry_count, created_at, started_at, completed_at, verify_folder
                    FROM queue_old
                    """)
                else:
                    await db.execute("""
                    INSERT INTO queue (id, task_type, status, src_side, src_relpath, dst_side, dst_relpath, 
                                     size_bytes, bytes_transferred, error_message, retry_count, created_at, started_at, completed_at)
                    SELECT id, task_type, status, src_side, src_relpath, dst_side, dst_relpath, 
                           size_bytes, bytes_transferred, error_message, retry_count, created_at, started_at, completed_at
                    FROM queue_old
                    """)
                
                # Drop old table
                await db.execute("DROP TABLE queue_old")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status)")
                print("Migration complete.")
    
    await init_db(db_path)
    
    # Enable WAL mode for better concurrency
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")


async def shutdown_db() -> None:
    """Cleanup database on shutdown."""
    settings = get_settings()
    db_path = settings.get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA optimize;")
