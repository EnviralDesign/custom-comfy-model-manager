"""API router for AI lookup job queue and review actions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import get_settings
from app.database import get_db
from app.services.source_manager import get_source_manager, ModelSource
from app.websocket import broadcast

router = APIRouter()


class AiLookupItem(BaseModel):
    filename: str
    relpath: Optional[str] = None
    file_hash: Optional[str] = None


class AiLookupEnqueueRequest(BaseModel):
    items: list[AiLookupItem]


class AiLookupJobResponse(BaseModel):
    id: int
    status: str
    decision: Optional[str] = None
    filename: str
    relpath: Optional[str] = None
    file_hash: Optional[str] = None
    model: Optional[str] = None
    found: bool = False
    accepted: bool = False
    candidate_url: Optional[str] = None
    candidate_source: Optional[str] = None
    candidate_notes: Optional[str] = None
    validation: Optional[dict[str, Any]] = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    error_message: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    decision_at: Optional[str] = None


class AiLookupEnqueueResponse(BaseModel):
    created: int
    skipped: int
    job_ids: list[int]
    existing_ids: list[int]


def _row_to_job(row: dict) -> dict:
    payload = dict(row)
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


@router.post("/lookup/jobs", response_model=AiLookupEnqueueResponse)
async def enqueue_ai_lookup_jobs(request: AiLookupEnqueueRequest):
    if not request.items:
        raise HTTPException(status_code=400, detail="No items provided")

    settings = get_settings()
    created = 0
    skipped = 0
    job_ids: list[int] = []
    existing_ids: list[int] = []
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        for item in request.items:
            filename = item.filename.strip()
            if not filename:
                skipped += 1
                continue

            relpath = item.relpath or None
            file_hash = item.file_hash or None

            cursor = await db.execute(
                """
                SELECT id FROM ai_lookup_jobs
                WHERE decision IS NULL
                AND status IN ('pending', 'running', 'completed')
                AND (
                    (relpath IS NOT NULL AND relpath = ?)
                    OR (file_hash IS NOT NULL AND file_hash = ?)
                )
                LIMIT 1
                """,
                (relpath, file_hash),
            )
            existing = await cursor.fetchone()
            if existing:
                skipped += 1
                existing_ids.append(existing["id"])
                continue

            steps = json.dumps(
                [
                    {
                        "time": now,
                        "source": "system",
                        "message": "Queued for lookup.",
                    }
                ]
            )

            cursor = await db.execute(
                """
                INSERT INTO ai_lookup_jobs (
                    status, filename, relpath, file_hash, model,
                    steps_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "pending",
                    filename,
                    relpath,
                    file_hash,
                    settings.xai_model,
                    steps,
                    now,
                ),
            )
            job_id = cursor.lastrowid
            job_ids.append(job_id)
            created += 1

        await db.commit()

    for job_id in job_ids:
        await _broadcast_job(job_id)

    return AiLookupEnqueueResponse(
        created=created,
        skipped=skipped,
        job_ids=job_ids,
        existing_ids=existing_ids,
    )


@router.get("/lookup/jobs", response_model=list[AiLookupJobResponse])
async def list_ai_lookup_jobs(include_decided: bool = Query(default=False)):
    async with get_db() as db:
        if include_decided:
            cursor = await db.execute(
                "SELECT * FROM ai_lookup_jobs ORDER BY created_at DESC"
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM ai_lookup_jobs WHERE decision IS NULL ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
    return [_row_to_job(dict(row)) for row in rows]


@router.post("/lookup/jobs/{job_id}/approve")
async def approve_ai_lookup_job(job_id: int):
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["decision"]:
        return {"status": "already_decided", "decision": job["decision"]}

    if not job.get("accepted") or not job.get("candidate_url"):
        raise HTTPException(status_code=400, detail="Job has no accepted URL to apply")

    source_mgr = get_source_manager()
    now = datetime.now(timezone.utc).isoformat()
    url = job["candidate_url"]

    if job.get("file_hash"):
        source = ModelSource(
            url=url,
            added_at=now,
            notes=None,
            filename_hint=job.get("filename"),
        )
        await source_mgr.set_source(job["file_hash"], source)
    elif job.get("relpath"):
        source = ModelSource(
            url=url,
            added_at=now,
            notes=None,
            filename_hint=job.get("filename"),
            relpath=job.get("relpath"),
        )
        await source_mgr.set_source_by_relpath(job["relpath"], source)

        # Queue hash for unhashed files
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT id FROM queue WHERE task_type='hash_file' AND src_relpath=? AND status IN ('pending', 'running')",
                (job["relpath"],),
            )
            if not await cursor.fetchone():
                await db.execute(
                    """
                    INSERT INTO queue (task_type, src_relpath, created_at, size_bytes)
                    VALUES (?, ?, ?, 0)
                    """,
                    ("hash_file", job["relpath"], now),
                )
                await db.commit()

    await _update_job(job_id, {"decision": "approved", "decision_at": now})
    await _broadcast_job(job_id)
    return {"status": "approved"}


@router.post("/lookup/jobs/{job_id}/reject")
async def reject_ai_lookup_job(job_id: int):
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["decision"]:
        return {"status": "already_decided", "decision": job["decision"]}

    now = datetime.now(timezone.utc).isoformat()
    await _update_job(job_id, {"decision": "rejected", "decision_at": now})
    await _broadcast_job(job_id)
    return {"status": "rejected"}


@router.post("/lookup/jobs/{job_id}/retry")
async def retry_ai_lookup_job(job_id: int):
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] not in ("failed", "completed", "cancelled"):
        raise HTTPException(status_code=400, detail="Job is not ready to retry")

    now = datetime.now(timezone.utc).isoformat()
    steps = json.dumps(
        [
            {
                "time": now,
                "source": "system",
                "message": "Queued for retry.",
            }
        ]
    )
    await _update_job(
        job_id,
        {
            "status": "pending",
            "decision": None,
            "decision_at": None,
            "found": 0,
            "accepted": 0,
            "candidate_url": None,
            "candidate_source": None,
            "candidate_notes": None,
            "validation_json": None,
            "steps_json": steps,
            "error_message": None,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
        },
    )
    await _broadcast_job(job_id)
    return {"status": "queued"}


@router.post("/lookup/jobs/{job_id}/cancel")
async def cancel_ai_lookup_job(job_id: int):
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] not in ("pending", "running"):
        return {"status": "not_cancellable"}

    now = datetime.now(timezone.utc).isoformat()
    await _update_job(
        job_id,
        {"status": "cancelled", "completed_at": now},
    )
    await _append_step(job_id, "Cancelled by user.", source="system")
    await _broadcast_job(job_id)
    return {"status": "cancelled"}


async def _get_job(job_id: int) -> dict | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM ai_lookup_jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _update_job(job_id: int, fields: dict[str, Any]) -> None:
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


async def _append_step(job_id: int, message: str, source: str = "system") -> None:
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


async def _broadcast_job(job_id: int) -> None:
    job = await _get_job(job_id)
    if not job:
        return
    await broadcast("ai_lookup_update", _row_to_job(job))
