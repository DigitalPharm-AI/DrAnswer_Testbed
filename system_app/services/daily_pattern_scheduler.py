from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.public_ids import require_public_id
from shared.time_utils import utc_now
from system_app.models import AgentJob, MedicationPlan
from system_app.services.agent_jobs import (
    DAILY_PATTERN_JOB_TYPE,
    DONE,
    PENDING,
)

DAILY_PATTERN_TRIGGER_TIME = time(2, 0)
DAILY_PATTERN_TIMEZONE = ZoneInfo("Asia/Seoul")


def ensure_due_daily_pattern_job(
    session: Session,
    *,
    current_time: datetime,
) -> AgentJob | None:
    """Persist the Backend-owned daily 02:00 dispatch exactly once.

    The testbed clock is stored as naive Asia/Seoul wall time. A timezone-aware
    value is normalized to Asia/Seoul so the same service can also be driven by
    a production scheduler.
    """

    backend_now = _backend_local_time(current_time)
    if backend_now.time() < DAILY_PATTERN_TRIGGER_TIME:
        return None

    analysis_date = backend_now.date() - timedelta(days=1)
    request_id = daily_pattern_request_id(analysis_date)
    existing = session.scalar(
        select(AgentJob).where(AgentJob.request_id == request_id)
    )
    if existing is not None:
        return existing

    patient_ids = active_daily_pattern_patient_ids(
        session,
        analysis_date=analysis_date,
    )
    payload = {
        "request_id": request_id,
        "patient_id": patient_ids,
        "analysis_date": analysis_date.isoformat(),
    }
    now = utc_now()
    job = AgentJob(
        request_id=request_id,
        job_type=DAILY_PATTERN_JOB_TYPE,
        status=PENDING if patient_ids else DONE,
        payload_json=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        error_message="" if patient_ids else "no_active_patients",
        completed_at=None if patient_ids else now,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job


def active_daily_pattern_patient_ids(
    session: Session,
    *,
    analysis_date: date,
) -> list[str]:
    """Return patients with an active medication plan on the analysis date."""

    values = session.scalars(
        select(MedicationPlan.patient_id)
        .where(
            MedicationPlan.active.is_(True),
            MedicationPlan.start_date <= analysis_date,
            MedicationPlan.end_date >= analysis_date,
        )
        .distinct()
        .order_by(MedicationPlan.patient_id.asc())
    ).all()
    return [
        require_public_id(str(patient_id), "patient")
        for patient_id in values
    ]


def daily_pattern_request_id(analysis_date: date) -> str:
    digest = hashlib.sha256(
        f"backend-daily-medication-pattern:{analysis_date.isoformat()}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return f"req_{digest}"


def _backend_local_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(DAILY_PATTERN_TIMEZONE).replace(tzinfo=None)
