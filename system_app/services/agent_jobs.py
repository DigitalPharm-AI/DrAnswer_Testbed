from __future__ import annotations

import json
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.schemas import DailyMedicationPattern, MissedDoseEventPayload
from shared.time_utils import utc_now
from system_app.models import AgentJob

AgentJobType = Literal["missed_dose", "daily_pattern"]

PENDING = "pending"
RUNNING = "running"
FAILED = "failed"
DONE = "done"
RETRY_REQUESTED = "retry_requested"


def create_agent_job(session: Session, job_type: AgentJobType, payload: MissedDoseEventPayload | DailyMedicationPattern) -> AgentJob:
    related_dose_event_id = getattr(payload, "dose_event_id", None)
    if job_type == "missed_dose" and related_dose_event_id is not None:
        existing = session.scalar(
            select(AgentJob)
            .where(
                AgentJob.job_type == job_type,
                AgentJob.related_dose_event_id == related_dose_event_id,
                AgentJob.status.in_([PENDING, RUNNING, DONE]),
            )
            .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        )
        if existing is not None:
            return existing
    job = AgentJob(
        job_type=job_type,
        status=PENDING,
        payload_json=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False),
        related_dose_event_id=related_dose_event_id,
    )
    session.add(job)
    session.flush()
    return job


def claim_next_agent_job(session: Session) -> AgentJob | None:
    job = session.scalar(
        select(AgentJob)
        .where(AgentJob.status == PENDING)
        .order_by(AgentJob.created_at.asc(), AgentJob.id.asc())
        .limit(1)
    )
    if job is None:
        return None
    job.status = RUNNING
    job.attempts += 1
    now = utc_now()
    job.started_at = now
    job.updated_at = now
    job.error_message = ""
    session.flush()
    return job


def reset_running_agent_jobs(session: Session) -> int:
    jobs = session.scalars(select(AgentJob).where(AgentJob.status.in_([RUNNING, RETRY_REQUESTED]))).all()
    now = utc_now()
    for job in jobs:
        if job.status == RETRY_REQUESTED:
            job.status = FAILED
            job.started_at = None
            job.completed_at = None
        else:
            job.status = PENDING
            job.error_message = "서버 재시작 후 대기 상태로 복구되었습니다."
        job.updated_at = now
    session.flush()
    return len(jobs)


def mark_agent_job_done(session: Session, job_id: int) -> None:
    job = session.get(AgentJob, job_id)
    if job is None:
        return
    job.status = DONE
    now = utc_now()
    job.updated_at = now
    job.completed_at = now
    job.error_message = ""
    session.flush()


def mark_agent_job_failed(session: Session, job_id: int, error_message: str) -> None:
    job = session.get(AgentJob, job_id)
    if job is None:
        return
    job.status = FAILED
    job.updated_at = utc_now()
    job.completed_at = None
    job.error_message = error_message
    session.flush()


def agent_job_runtime_metadata(job: AgentJob | None) -> dict[str, object]:
    if job is None:
        return {}
    return {
        "agent_job_id": job.id,
        "agent_job_type": job.job_type,
        "agent_job_status": job.status,
        "agent_job_attempts": job.attempts,
        "agent_job_retryable": job.status == FAILED,
        "agent_job_error_message": job.error_message,
    }


def retry_agent_job(session: Session, job_id: int) -> AgentJob | None:
    job = session.get(AgentJob, job_id)
    if job is None or job.status != FAILED:
        return job
    job.status = PENDING
    job.updated_at = utc_now()
    job.started_at = None
    job.completed_at = None
    job.error_message = ""
    session.flush()
    return job


def mark_agent_job_retry_requested(session: Session, job_id: int) -> AgentJob | None:
    job = session.get(AgentJob, job_id)
    if job is None or job.status != FAILED:
        return job
    job.status = RETRY_REQUESTED
    job.updated_at = utc_now()
    session.flush()
    return job


def complete_agent_job_retry_request(session: Session, job_id: int) -> AgentJob | None:
    job = session.get(AgentJob, job_id)
    if job is None or job.status != RETRY_REQUESTED:
        return job
    job.status = PENDING
    job.updated_at = utc_now()
    job.started_at = None
    job.completed_at = None
    job.error_message = ""
    session.flush()
    return job


def restore_agent_job_retry_failure(session: Session, job_id: int) -> AgentJob | None:
    job = session.get(AgentJob, job_id)
    if job is None or job.status != RETRY_REQUESTED:
        return job
    job.status = FAILED
    job.updated_at = utc_now()
    job.started_at = None
    job.completed_at = None
    session.flush()
    return job


def deserialize_agent_job_payload(job: AgentJob) -> MissedDoseEventPayload | DailyMedicationPattern:
    payload = json.loads(job.payload_json)
    if job.job_type == "missed_dose":
        return MissedDoseEventPayload.model_validate(payload)
    if job.job_type == "daily_pattern":
        return DailyMedicationPattern.model_validate(payload)
    raise ValueError(f"unsupported agent job type: {job.job_type}")
