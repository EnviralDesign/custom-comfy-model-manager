"""Background worker for AI source URL lookup jobs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.database import get_db
from app.websocket import broadcast
from app.services.ai_lookup_service import (
    call_xai_lookup,
    check_url_sync,
    filename_matches_url,
)


class AiLookupWorker:
    _instance = None
    _running = False

    def __init__(self) -> None:
        self.settings = get_settings()
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    def get_instance(cls) -> "AiLookupWorker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self) -> None:
        if AiLookupWorker._running:
            return
        AiLookupWorker._running = True
        asyncio.create_task(self._worker_loop())
        print("✓ AI lookup worker started")

    async def stop(self) -> None:
        AiLookupWorker._running = False
        for task in list(self._tasks):
            task.cancel()
        print("AI lookup worker stopped")

    async def _worker_loop(self) -> None:
        while AiLookupWorker._running:
            try:
                await self._launch_pending_jobs()
            except Exception as exc:
                print(f"AI lookup worker error: {exc}")
            await asyncio.sleep(1)

    async def _launch_pending_jobs(self) -> None:
        concurrency = max(1, int(self.settings.xai_lookup_concurrency or 1))
        available = concurrency - len(self._tasks)
        if available <= 0:
            return

        jobs = await self._get_pending_jobs(limit=available)
        for job in jobs:
            job_id = job["id"]
            if not await self._mark_running(job_id):
                continue
            task = asyncio.create_task(self._run_job(job_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _get_pending_jobs(self, limit: int) -> list[dict]:
        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT * FROM ai_lookup_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _mark_running(self, job_id: int) -> bool:
        async with get_db() as db:
            cursor = await db.execute(
                """
                UPDATE ai_lookup_jobs
                SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            await db.commit()
        if cursor.rowcount == 0:
            return False
        await self._append_step(job_id, "Search started.", source="system")
        await self._broadcast_job(job_id)
        return True

    async def _run_job(self, job_id: int) -> None:
        try:
            job = await self._get_job(job_id)
            if not job:
                return

            if not self.settings.xai_api_key:
                await self._fail_job(job_id, "XAI_API_KEY is not configured.")
                return

            await self._append_step(job_id, "Calling Grok with web search...", source="system")

            result = await asyncio.to_thread(
                call_xai_lookup,
                base_url=self.settings.xai_api_base_url,
                api_key=self.settings.xai_api_key or "",
                model=self.settings.xai_model,
                filename=job["filename"],
                relpath=job.get("relpath"),
            )

            if await self._is_cancelled(job_id):
                await self._append_step(job_id, "Cancelled before result processing.", source="system")
                await self._broadcast_job(job_id)
                return

            if result.get("error"):
                await self._fail_job(job_id, result["error"])
                return

            candidate_url = (result.get("url") or "").strip()
            found = bool(result.get("found")) and bool(candidate_url)
            model_steps = result.get("steps") or []

            for step in model_steps:
                await self._append_step(job_id, step, source="model")

            if not found:
                await self._complete_job(
                    job_id,
                    found=0,
                    accepted=0,
                    notes="No exact filename match found.",
                )
                return

            if not filename_matches_url(job["filename"], candidate_url):
                await self._complete_job(
                    job_id,
                    found=0,
                    accepted=0,
                    notes="Candidate URL filename does not match exactly.",
                )
                return

            await self._append_step(job_id, "Validating candidate URL...", source="system")
            validation = await asyncio.to_thread(check_url_sync, candidate_url)

            if not validation.get("ok"):
                await self._complete_job(
                    job_id,
                    found=1,
                    accepted=0,
                    candidate_url=candidate_url,
                    candidate_source=result.get("source") or "",
                    candidate_notes=result.get("notes") or "",
                    validation=validation,
                    notes="Candidate URL failed validation.",
                )
                return

            await self._complete_job(
                job_id,
                found=1,
                accepted=1,
                candidate_url=candidate_url,
                candidate_source=result.get("source") or "",
                candidate_notes=result.get("notes") or "",
                validation=validation,
                notes="Exact match found and validated.",
            )
        except asyncio.CancelledError:
            await self._fail_job(job_id, "Lookup cancelled.")
        except Exception as exc:
            await self._fail_job(job_id, str(exc))

    async def _get_job(self, job_id: int) -> dict | None:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM ai_lookup_jobs WHERE id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def _is_cancelled(self, job_id: int) -> bool:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT status FROM ai_lookup_jobs WHERE id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
        return bool(row and row["status"] == "cancelled")

    async def _append_step(self, job_id: int, message: str, source: str = "system") -> None:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT steps_json FROM ai_lookup_jobs WHERE id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            steps = []
            if row and row["steps_json"]:
                try:
                    steps = json.loads(row["steps_json"])
                except json.JSONDecodeError:
                    steps = []
            steps.append(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "source": source,
                    "message": message,
                }
            )
            await db.execute(
                "UPDATE ai_lookup_jobs SET steps_json = ? WHERE id = ?",
                (json.dumps(steps), job_id),
            )
            await db.commit()
        await self._broadcast_job(job_id)

    async def _complete_job(
        self,
        job_id: int,
        *,
        found: int,
        accepted: int,
        candidate_url: str | None = None,
        candidate_source: str | None = None,
        candidate_notes: str | None = None,
        validation: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> None:
        fields = {
            "status": "completed",
            "found": found,
            "accepted": accepted,
            "candidate_url": candidate_url,
            "candidate_source": candidate_source,
            "candidate_notes": candidate_notes or notes,
            "validation_json": json.dumps(validation) if validation else None,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error_message": None,
        }
        await self._update_job(job_id, fields)
        if notes:
            await self._append_step(job_id, notes, source="system")
        await self._broadcast_job(job_id)

    async def _fail_job(self, job_id: int, error_message: str) -> None:
        fields = {
            "status": "failed",
            "error_message": error_message,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._update_job(job_id, fields)
        await self._append_step(job_id, f"Failed: {error_message}", source="system")
        await self._broadcast_job(job_id)

    async def _update_job(self, job_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        columns = ", ".join([f"{k} = ?" for k in fields.keys()])
        values = list(fields.values())
        values.append(job_id)
        async with get_db() as db:
            await db.execute(
                f"UPDATE ai_lookup_jobs SET {columns} WHERE id = ?",
                values,
            )
            await db.commit()

    async def _broadcast_job(self, job_id: int) -> None:
        job = await self._get_job(job_id)
        if not job:
            return
        payload = self._serialize_job(job)
        await broadcast("ai_lookup_update", payload)

    def _serialize_job(self, job: dict) -> dict:
        payload = dict(job)
        payload["found"] = bool(payload.get("found"))
        payload["accepted"] = bool(payload.get("accepted"))

        steps = []
        if payload.get("steps_json"):
            try:
                steps = json.loads(payload["steps_json"])
            except json.JSONDecodeError:
                steps = []
        payload["steps"] = steps

        if payload.get("validation_json"):
            try:
                payload["validation"] = json.loads(payload["validation_json"])
            except json.JSONDecodeError:
                payload["validation"] = None
        else:
            payload["validation"] = None

        payload.pop("steps_json", None)
        payload.pop("validation_json", None)
        return payload


def get_ai_lookup_worker() -> AiLookupWorker:
    return AiLookupWorker.get_instance()
