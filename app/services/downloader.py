"""Resumable downloader with aggressive stall recovery."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlparse

import requests

from app.config import get_settings
from app.database import get_db
from app.services.source_manager import ModelSource, get_source_manager


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _sanitize_filename(name: str) -> str:
    # Keep only the basename and drop accidental appended disposition params.
    cleaned = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().strip('"').strip("'")
    cleaned = re.split(r";\s*filename\*?=", cleaned, maxsplit=1, flags=re.IGNORECASE)[0].strip()

    # Windows-unfriendly chars + ';' to prevent parameter-like tails in filenames.
    invalid = '<>:"/\\|?*;'
    cleaned = "".join("_" if c in invalid or ord(c) < 32 else c for c in cleaned).strip()
    cleaned = cleaned.rstrip(" .")
    return cleaned or "download.bin"


def _url_basename(url: str) -> str:
    path = urlparse(url).path
    if not path:
        return ""
    return unquote(path.rsplit("/", 1)[-1])


def _parse_content_disposition(header_value: str) -> str | None:
    if not header_value:
        return None
    # RFC 5987 / 6266 preferred field (capture quoted or unquoted token up to ';').
    m_star = re.search(r"filename\*\s*=\s*(?:\"([^\"]*)\"|([^;]+))", header_value, flags=re.IGNORECASE)
    if m_star:
        value = (m_star.group(1) or m_star.group(2) or "").strip()
        if "''" in value:
            charset, encoded = value.split("''", 1)
            try:
                return unquote_to_bytes(encoded).decode(charset or "utf-8", errors="replace")
            except Exception:
                return unquote(encoded)
        return unquote(value)

    m_name = re.search(r"filename\s*=\s*(?:\"([^\"]*)\"|([^;]+))", header_value, flags=re.IGNORECASE)
    if m_name:
        return (m_name.group(1) or m_name.group(2) or "").strip()
    return None


def _detect_provider(url: str) -> str:
    host = urlparse(url).hostname or ""
    host = host.lower()
    if "civitai.com" in host:
        return "civitai"
    if "huggingface.co" in host or host.endswith("hf.co"):
        return "huggingface"
    return "generic"


@dataclass
class DownloadJob:
    id: int
    url: str
    filename: str | None = None
    provider: str = "generic"
    status: str = "queued"
    bytes_downloaded: int = 0
    total_bytes: int | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    error_message: str | None = None
    attempts: int = 0
    dest_path: Path | None = None
    temp_path: Path | None = None
    target_root: Path | None = None
    record_source: bool = False
    cancelled: bool = False
    force_start: bool = False
    last_persist_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "provider": self.provider,
            "status": self.status,
            "bytes_downloaded": self.bytes_downloaded,
            "total_bytes": self.total_bytes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_message": self.error_message,
            "attempts": self.attempts,
            "dest_path": str(self.dest_path) if self.dest_path else None,
        }


class DownloadManager:
    _instance: "DownloadManager | None" = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[int, DownloadJob] = {}
        self._active: set[int] = set()
        self._next_id = 1
        self._session = requests.Session()
        self._running = True
        self._loaded = False
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    @classmethod
    def get_instance(cls) -> "DownloadManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def load_persisted_jobs(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        settings = get_settings()
        downloads_dir = settings.get_downloads_dir().resolve()
        now = _now_iso()

        async with get_db() as db:
            await db.execute(
                "UPDATE download_jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (now,),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM download_jobs WHERE status IN ('queued', 'running')"
            )
            rows = await cursor.fetchall()

        max_id = 0
        for row in rows:
            job_id = row["id"]
            max_id = max(max_id, job_id)

            dest_path = Path(row["dest_path"]) if row["dest_path"] else None
            temp_path = Path(row["temp_path"]) if row["temp_path"] else None
            target_root = Path(row["target_root"]) if row["target_root"] else None
            record_source = bool(row["record_source"])

            invalid_reason = None
            if not dest_path:
                invalid_reason = "missing destination path"
            elif target_root:
                try:
                    if not str(dest_path.resolve()).startswith(str(target_root.resolve())):
                        invalid_reason = "destination no longer under target root"
                except Exception:
                    invalid_reason = "invalid destination path"
            else:
                try:
                    if not str(dest_path.resolve()).startswith(str(downloads_dir)):
                        invalid_reason = "destination no longer under downloads directory"
                except Exception:
                    invalid_reason = "invalid destination path"

            if invalid_reason:
                async with get_db() as db:
                    await db.execute(
                        """
                        UPDATE download_jobs
                        SET status = 'failed', error_message = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (invalid_reason, now, job_id),
                    )
                    await db.commit()
                continue

            if not temp_path and dest_path:
                temp_path = dest_path.with_suffix(dest_path.suffix + ".part")

            bytes_downloaded = row["bytes_downloaded"] or 0
            if temp_path and temp_path.exists():
                try:
                    bytes_downloaded = temp_path.stat().st_size
                except Exception:
                    pass

            job = DownloadJob(
                id=job_id,
                url=row["url"],
                filename=row["filename"],
                provider=row["provider"],
                status="queued",
                bytes_downloaded=bytes_downloaded,
                total_bytes=row["total_bytes"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                error_message=row["error_message"],
                attempts=row["attempts"] or 0,
                dest_path=dest_path,
                temp_path=temp_path,
                target_root=target_root,
                record_source=record_source,
            )

            self._jobs[job_id] = job

        if max_id >= self._next_id:
            self._next_id = max_id + 1

    async def _persist_job(self, job: DownloadJob) -> None:
        async with get_db() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO download_jobs
                (id, url, filename, provider, status, bytes_downloaded, total_bytes,
                 created_at, updated_at, error_message, attempts, dest_path, temp_path,
                 target_root, record_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.url,
                    job.filename,
                    job.provider,
                    job.status,
                    job.bytes_downloaded,
                    job.total_bytes,
                    job.created_at,
                    job.updated_at,
                    job.error_message,
                    job.attempts,
                    str(job.dest_path) if job.dest_path else None,
                    str(job.temp_path) if job.temp_path else None,
                    str(job.target_root) if job.target_root else None,
                    1 if job.record_source else 0,
                ),
            )
            await db.commit()

    def _persist_job_sync(self, job: DownloadJob) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._persist_job(job))
            return
        loop.create_task(self._persist_job(job))

    def list_jobs(self) -> list[DownloadJob]:
        with self._lock:
            return list(self._jobs.values())

    def get_job(self, job_id: int) -> DownloadJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel_job(self, job_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.cancelled = True
            job.status = "cancelled"
            job.updated_at = _now_iso()
            self._persist_job_sync(job)
            return True

    def cancel_all(self) -> int:
        count = 0
        with self._lock:
            for job in self._jobs.values():
                if job.status in ("completed", "failed", "cancelled"):
                    continue
                job.cancelled = True
                job.status = "cancelled"
                job.updated_at = _now_iso()
                self._persist_job_sync(job)
                count += 1
        return count

    def create_job(
        self,
        *,
        url: str,
        filename: str | None = None,
        provider: str | None = None,
        start_now: bool = False,
        dest_dir: Path | None = None,
        target_root: Path | None = None,
        record_source: bool = False,
    ) -> DownloadJob:
        settings = get_settings()
        downloads_dir = settings.get_downloads_dir()

        if not provider or provider == "auto":
            provider = _detect_provider(url)

        if not filename:
            filename = _url_basename(url) or f"download-{self._next_id}.bin"

        filename = _sanitize_filename(filename)
        dest_root = dest_dir or downloads_dir
        dest_path = dest_root / filename
        temp_path = dest_path.with_suffix(dest_path.suffix + ".part")

        with self._lock:
            job_id = self._next_id
            self._next_id += 1
            job = DownloadJob(
                id=job_id,
                url=url,
                filename=filename,
                provider=provider,
                dest_path=dest_path,
                temp_path=temp_path,
                target_root=target_root,
                record_source=record_source,
                force_start=bool(start_now),
            )
            self._jobs[job_id] = job
            self._persist_job_sync(job)
        if start_now:
            self.start_job(job_id, force=True)
        else:
            self.start_job(job_id, force=False)
        return job

    def start_job(self, job_id: int, force: bool = False) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in ("running", "completed", "failed", "cancelled"):
                return False
            if not force:
                settings = get_settings()
                max_concurrent = max(1, int(settings.downloader_max_concurrent))
                if len(self._active) >= max_concurrent:
                    job.status = "queued"
                    job.updated_at = _now_iso()
                    self._persist_job_sync(job)
                    return False
            job.force_start = force
            self._start_job_locked(job)
            return True

    def _start_job_locked(self, job: DownloadJob) -> None:
        job.status = "running"
        job.updated_at = _now_iso()
        self._active.add(job.id)
        self._persist_job_sync(job)
        threading.Thread(target=self._run_job, args=(job.id,), daemon=True).start()

    def _scheduler_loop(self) -> None:
        while self._running:
            try:
                with self._lock:
                    settings = get_settings()
                    max_concurrent = max(1, int(settings.downloader_max_concurrent))
                    if len(self._active) < max_concurrent:
                        queued = [j for j in self._jobs.values() if j.status == "queued" and not j.cancelled]
                        queued.sort(key=lambda j: j.id)
                        for job in queued:
                            if len(self._active) >= max_concurrent:
                                break
                            self._start_job_locked(job)
            except Exception:
                pass
            time.sleep(0.5)

    def _resolve_auth_header(self, job: DownloadJob) -> dict[str, str]:
        settings = get_settings()
        if job.provider == "civitai":
            token = settings.civitai_api_key
        elif job.provider == "huggingface":
            token = settings.huggingface_api_key
        else:
            token = None

        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    async def _record_source_url(self, job: DownloadJob) -> None:
        if not job.record_source or not job.target_root or not job.dest_path:
            return
        try:
            relpath = job.dest_path.relative_to(job.target_root)
        except Exception:
            return

        relpath_text = relpath.as_posix()
        source_mgr = get_source_manager()
        source = ModelSource(
            url=job.url,
            added_at=datetime.now(timezone.utc).isoformat(),
            filename_hint=job.filename,
            relpath=relpath_text,
        )
        await source_mgr.set_source_by_relpath(relpath_text, source)

        async with get_db() as db:
            try:
                stat = job.dest_path.stat()
            except Exception:
                stat = None

            if stat is not None:
                cursor = await db.execute(
                    "SELECT hash FROM file_index WHERE side = 'local' AND relpath = ?",
                    (relpath_text,),
                )
                row = await cursor.fetchone()
                if not row:
                    await db.execute(
                        """
                        INSERT INTO file_index (side, relpath, size, mtime_ns, hash, hash_computed_at, indexed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "local",
                            relpath_text,
                            stat.st_size,
                            stat.st_mtime_ns,
                            None,
                            None,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )

            cursor = await db.execute(
                "SELECT id FROM queue WHERE task_type='hash_file' AND src_relpath=? AND status IN ('pending', 'running')",
                (relpath_text,),
            )
            if not await cursor.fetchone():
                await db.execute(
                    """
                    INSERT INTO queue (task_type, src_relpath, created_at, size_bytes)
                    VALUES (?, ?, ?, 0)
                    """,
                    ("hash_file", relpath_text, datetime.now(timezone.utc).isoformat()),
                )
            await db.commit()

    def _post_complete(self, job: DownloadJob) -> None:
        if job.record_source and job.target_root:
            try:
                asyncio.run(self._record_source_url(job))
            except Exception:
                pass

    def _run_job(self, job_id: int) -> None:
        job = self.get_job(job_id)
        if not job:
            return

        settings = get_settings()
        connect_timeout = max(1, int(settings.downloader_connect_timeout_seconds))
        stall_timeout = max(1, int(settings.downloader_stall_timeout_seconds))

        try:
            while not job.cancelled:
                job.attempts += 1
                job.updated_at = _now_iso()
                job.status = "running"
                job.error_message = None
                self._persist_job_sync(job)

                if not job.temp_path:
                    job.temp_path = job.dest_path.with_suffix(job.dest_path.suffix + ".part")

                if job.dest_path:
                    job.dest_path.parent.mkdir(parents=True, exist_ok=True)

                existing_size = job.temp_path.stat().st_size if job.temp_path.exists() else 0
                headers = {
                    "User-Agent": "ComfyDownloader/0.1",
                }
                headers.update(self._resolve_auth_header(job))
                if existing_size > 0:
                    headers["Range"] = f"bytes={existing_size}-"

                try:
                    with self._session.get(
                        job.url,
                        headers=headers,
                        stream=True,
                        allow_redirects=True,
                        timeout=(connect_timeout, stall_timeout),
                    ) as resp:
                        if resp.status_code >= 400:
                            job.status = "failed"
                            job.error_message = f"HTTP {resp.status_code}"
                            job.updated_at = _now_iso()
                            self._persist_job_sync(job)
                            return

                        suggested_name = _parse_content_disposition(resp.headers.get("Content-Disposition", ""))
                        if suggested_name and suggested_name != job.filename:
                            suggested_name = _sanitize_filename(suggested_name)
                            new_dest = job.dest_path.parent / suggested_name
                            new_temp = new_dest.with_suffix(new_dest.suffix + ".part")
                            if job.temp_path.exists():
                                job.temp_path.rename(new_temp)
                            job.filename = suggested_name
                            job.dest_path = new_dest
                            job.temp_path = new_temp
                            existing_size = job.temp_path.stat().st_size if job.temp_path.exists() else 0

                        mode = "ab" if existing_size > 0 else "wb"
                        if existing_size > 0 and resp.status_code == 200:
                            existing_size = 0
                            mode = "wb"

                        content_length = resp.headers.get("Content-Length")
                        if content_length and content_length.isdigit():
                            total_size = int(content_length) + existing_size
                        else:
                            total_size = None

                        job.total_bytes = total_size
                        job.bytes_downloaded = existing_size

                        with open(job.temp_path, mode) as handle:
                            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                                if job.cancelled:
                                    job.status = "cancelled"
                                    job.updated_at = _now_iso()
                                    return
                                if not chunk:
                                    continue
                                handle.write(chunk)
                                job.bytes_downloaded += len(chunk)
                                job.updated_at = _now_iso()
                                now_ts = time.time()
                                if now_ts - job.last_persist_ts > 1.0:
                                    job.last_persist_ts = now_ts
                                    self._persist_job_sync(job)

                        # Completed?
                        if job.total_bytes and job.bytes_downloaded >= job.total_bytes:
                            job.temp_path.replace(job.dest_path)
                            job.status = "completed"
                            job.updated_at = _now_iso()
                            self._persist_job_sync(job)
                            self._post_complete(job)
                            return

                        # If total is unknown, assume completion when server closes
                        if not job.total_bytes:
                            job.temp_path.replace(job.dest_path)
                            job.status = "completed"
                            job.updated_at = _now_iso()
                            self._persist_job_sync(job)
                            self._post_complete(job)
                            return

                except requests.exceptions.ReadTimeout:
                    job.error_message = "stall_timeout"
                except requests.exceptions.ConnectionError:
                    job.error_message = "connection_error"
                except Exception as exc:
                    job.status = "failed"
                    job.error_message = str(exc)
                    job.updated_at = _now_iso()
                    self._persist_job_sync(job)
                    return

                # Retry after a brief pause
                time.sleep(0.5)

            job.status = "cancelled"
            job.updated_at = _now_iso()
            self._persist_job_sync(job)
        finally:
            with self._lock:
                self._active.discard(job_id)


_downloader_instance: DownloadManager | None = None


def get_download_manager() -> DownloadManager:
    global _downloader_instance
    if _downloader_instance is None:
        _downloader_instance = DownloadManager()
    return _downloader_instance
