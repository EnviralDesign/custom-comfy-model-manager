"""Resumable downloader with aggressive stall recovery."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from app.config import get_settings


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _sanitize_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if c in invalid else c for c in name).strip()
    return cleaned or "download.bin"


def _url_basename(url: str) -> str:
    path = urlparse(url).path
    if not path:
        return ""
    return unquote(path.rsplit("/", 1)[-1])


def _parse_content_disposition(header_value: str) -> str | None:
    if not header_value:
        return None
    header = header_value.strip()
    # RFC 5987: filename*=UTF-8''...
    if "filename*=" in header:
        parts = header.split("filename*=", 1)[1]
        parts = parts.strip().strip(";")
        if "''" in parts:
            _, encoded = parts.split("''", 1)
        else:
            encoded = parts
        return unquote(encoded.strip().strip('"'))
    if "filename=" in header:
        parts = header.split("filename=", 1)[1]
        return parts.strip().strip(";").strip('"')
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
    api_key_override: str | None = None
    cancelled: bool = False
    force_start: bool = False

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
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    @classmethod
    def get_instance(cls) -> "DownloadManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

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
                count += 1
        return count

    def create_job(
        self,
        *,
        url: str,
        filename: str | None = None,
        provider: str | None = None,
        api_key_override: str | None = None,
        start_now: bool = False,
    ) -> DownloadJob:
        settings = get_settings()
        downloads_dir = settings.get_downloads_dir()

        if not provider or provider == "auto":
            provider = _detect_provider(url)

        if not filename:
            filename = _url_basename(url) or f"download-{self._next_id}.bin"

        filename = _sanitize_filename(filename)
        dest_path = downloads_dir / filename
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
                api_key_override=api_key_override,
                force_start=bool(start_now),
            )
            self._jobs[job_id] = job
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
                    return False
            job.force_start = force
            self._start_job_locked(job)
            return True

    def _start_job_locked(self, job: DownloadJob) -> None:
        job.status = "running"
        job.updated_at = _now_iso()
        self._active.add(job.id)
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
        token = job.api_key_override

        if job.provider == "civitai":
            token = token or settings.civitai_api_key
        elif job.provider == "huggingface":
            token = token or settings.huggingface_api_key

        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

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

                if not job.temp_path:
                    job.temp_path = job.dest_path.with_suffix(job.dest_path.suffix + ".part")

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

                        # Completed?
                        if job.total_bytes and job.bytes_downloaded >= job.total_bytes:
                            job.temp_path.replace(job.dest_path)
                            job.status = "completed"
                            job.updated_at = _now_iso()
                            return

                        # If total is unknown, assume completion when server closes
                        if not job.total_bytes:
                            job.temp_path.replace(job.dest_path)
                            job.status = "completed"
                            job.updated_at = _now_iso()
                            return

                except requests.exceptions.ReadTimeout:
                    job.error_message = "stall_timeout"
                except requests.exceptions.ConnectionError:
                    job.error_message = "connection_error"
                except Exception as exc:
                    job.status = "failed"
                    job.error_message = str(exc)
                    job.updated_at = _now_iso()
                    return

                # Retry after a brief pause
                time.sleep(0.5)

            job.status = "cancelled"
            job.updated_at = _now_iso()
        finally:
            with self._lock:
                self._active.discard(job_id)


_downloader_instance: DownloadManager | None = None


def get_download_manager() -> DownloadManager:
    global _downloader_instance
    if _downloader_instance is None:
        _downloader_instance = DownloadManager()
    return _downloader_instance
