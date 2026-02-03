"""Source URL Manager - stores hash -> public URL mappings in SQLite."""

from typing import Optional, Dict
from pydantic import BaseModel

from app.database import get_db


class ModelSource(BaseModel):
    url: str
    added_at: str
    notes: Optional[str] = None
    filename_hint: Optional[str] = None
    relpath: Optional[str] = None  # For unhashed files


class SourceManager:
    """Manages source URL mappings in SQLite database."""
    
    async def get_source(self, file_hash: str) -> Optional[ModelSource]:
        """Get source URL by hash."""
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT url, added_at, notes, filename_hint, relpath FROM source_urls WHERE key = ?",
                (file_hash,)
            )
            row = await cursor.fetchone()
            if row:
                return ModelSource(
                    url=row["url"],
                    added_at=row["added_at"],
                    notes=row["notes"],
                    filename_hint=row["filename_hint"],
                    relpath=row["relpath"],
                )
        return None

    async def get_source_by_relpath(self, relpath: str) -> Optional[tuple[str, ModelSource]]:
        """Get source by relpath (for unhashed files). Returns (key, source) tuple."""
        key = f"relpath:{relpath}"
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT key, url, added_at, notes, filename_hint, relpath FROM source_urls WHERE key = ?",
                (key,)
            )
            row = await cursor.fetchone()
            if row:
                return (row["key"], ModelSource(
                    url=row["url"],
                    added_at=row["added_at"],
                    notes=row["notes"],
                    filename_hint=row["filename_hint"],
                    relpath=row["relpath"],
                ))
        return None

    async def set_source(self, file_hash: str, source: ModelSource):
        """Set or update source URL by hash."""
        async with get_db() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO source_urls (key, url, added_at, notes, filename_hint, relpath)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (file_hash, source.url, source.added_at, source.notes, source.filename_hint, source.relpath)
            )
            await db.commit()

    async def set_source_by_relpath(self, relpath: str, source: ModelSource):
        """Set source by relpath (for unhashed files)."""
        key = f"relpath:{relpath}"
        source.relpath = relpath
        async with get_db() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO source_urls (key, url, added_at, notes, filename_hint, relpath)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, source.url, source.added_at, source.notes, source.filename_hint, relpath)
            )
            await db.commit()

    async def migrate_relpath_to_hash(self, relpath: str, file_hash: str):
        """Migrate a relpath-based entry to hash-based when hash is computed."""
        old_key = f"relpath:{relpath}"
        async with get_db() as db:
            # Check if relpath-based entry exists
            cursor = await db.execute(
                "SELECT url, added_at, notes, filename_hint FROM source_urls WHERE key = ?",
                (old_key,)
            )
            row = await cursor.fetchone()
            if row:
                # Insert with new hash key (or update if hash key already exists)
                await db.execute(
                    """
                    INSERT OR REPLACE INTO source_urls (key, url, added_at, notes, filename_hint, relpath)
                    VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (file_hash, row["url"], row["added_at"], row["notes"], row["filename_hint"])
                )
                # Delete old relpath-based entry
                await db.execute("DELETE FROM source_urls WHERE key = ?", (old_key,))
                await db.commit()
                print(f"Migrated source URL from relpath to hash: {file_hash[:8]}...")

    async def remove_source(self, file_hash: str):
        """Remove a source URL by hash."""
        async with get_db() as db:
            await db.execute("DELETE FROM source_urls WHERE key = ?", (file_hash,))
            await db.commit()

    async def remove_source_by_relpath(self, relpath: str):
        """Remove a source URL by relpath."""
        key = f"relpath:{relpath}"
        async with get_db() as db:
            await db.execute("DELETE FROM source_urls WHERE key = ?", (key,))
            await db.commit()

    async def get_all_sources(self) -> Dict[str, ModelSource]:
        """Get all source URLs."""
        result = {}
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT key, url, added_at, notes, filename_hint, relpath FROM source_urls"
            )
            for row in await cursor.fetchall():
                result[row["key"]] = ModelSource(
                    url=row["url"],
                    added_at=row["added_at"],
                    notes=row["notes"],
                    filename_hint=row["filename_hint"],
                    relpath=row["relpath"],
                )
        return result


# Singleton instance
_source_manager: Optional[SourceManager] = None

def get_source_manager() -> SourceManager:
    global _source_manager
    if _source_manager is None:
        _source_manager = SourceManager()
    return _source_manager
