"""Agent trace runner for interactive debugging."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import get_settings
from app.services.ai_tool_agent import run_tool_agent_lookup


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


@dataclass
class AgentTraceJob:
    id: int
    query: str
    filename: str
    file_hash: str | None = None
    relpath: str | None = None
    require_exact_filename: bool = True
    status: str = "queued"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    trace: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    cancelled: bool = False

    def to_dict(self, include_trace: bool = True) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "query": self.query,
            "filename": self.filename,
            "file_hash": self.file_hash,
            "relpath": self.relpath,
            "require_exact_filename": self.require_exact_filename,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }
        if include_trace:
            payload["trace"] = self.trace
        return payload


class AgentTraceManager:
    _instance: "AgentTraceManager | None" = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[int, AgentTraceJob] = {}
        self._next_id = 1

    @classmethod
    def get_instance(cls) -> "AgentTraceManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def list_jobs(self) -> list[AgentTraceJob]:
        with self._lock:
            return list(self._jobs.values())

    def get_job(self, job_id: int) -> AgentTraceJob | None:
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

    def create_job(
        self,
        *,
        query: str,
        file_hash: str | None,
        relpath: str | None,
        require_exact_filename: bool,
        max_steps: int | None = None,
    ) -> AgentTraceJob:
        with self._lock:
            job_id = self._next_id
            self._next_id += 1
            job = AgentTraceJob(
                id=job_id,
                query=query,
                filename=query,
                file_hash=file_hash,
                relpath=relpath,
                require_exact_filename=require_exact_filename,
            )
            self._jobs[job_id] = job

        threading.Thread(
            target=self._run_job,
            args=(job_id, max_steps),
            daemon=True,
        ).start()
        return job

    def _run_job(self, job_id: int, max_steps: int | None) -> None:
        job = self.get_job(job_id)
        if not job:
            return

        settings = get_settings()
        if not settings.xai_api_key:
            job.status = "failed"
            job.error = "XAI_API_KEY is not configured."
            job.updated_at = _now_iso()
            return

        def trace_callback(entry: dict[str, Any]):
            with self._lock:
                job.trace.append(entry)
                job.updated_at = _now_iso()

        def should_cancel() -> bool:
            return job.cancelled

        job.status = "running"
        job.updated_at = _now_iso()

        try:
            result = run_tool_agent_lookup(
                base_url=settings.xai_api_base_url,
                api_key=settings.xai_api_key or "",
                model=settings.xai_model,
                filename=job.filename,
                relpath=job.relpath,
                file_hash=job.file_hash,
                civitai_base_url=settings.civitai_api_base_url,
                civitai_api_key=settings.civitai_api_key,
                huggingface_api_key=settings.huggingface_api_key,
                max_steps=max_steps or settings.ai_tool_max_steps,
                require_exact_filename=job.require_exact_filename,
                trace_callback=trace_callback,
                should_cancel=should_cancel,
            )
            with self._lock:
                if job.cancelled:
                    job.status = "cancelled"
                else:
                    job.status = "completed" if result.get("found") else "no_match"
                job.result = result
                job.updated_at = _now_iso()
        except Exception as exc:
            with self._lock:
                job.status = "failed"
                job.error = str(exc)
                job.updated_at = _now_iso()


_agent_trace_instance: AgentTraceManager | None = None


def get_agent_trace_manager() -> AgentTraceManager:
    global _agent_trace_instance
    if _agent_trace_instance is None:
        _agent_trace_instance = AgentTraceManager()
    return _agent_trace_instance
