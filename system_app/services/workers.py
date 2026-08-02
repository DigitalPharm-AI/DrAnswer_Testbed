from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta

from sqlalchemy import desc, select

from shared.async_v13_contracts import (
    DailyMedicationPatternAnalysisRequest,
    MissedDoseEventRequest,
)
from shared.json_utils import dump_json as dump_metadata_json
from shared.json_utils import parse_json_object as parse_metadata_json
from shared.redaction import safe_exception_summary
from shared.schemas import MissedDoseEventPayload
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.db import SessionLocal
from system_app.models import AgentJob, DoseEvent, Notification
from system_app.services.agent_client import AgentClient
from system_app.services.agent_error_service import present_agent_error
from system_app.services.agent_jobs import (
    DAILY_PATTERN_JOB_TYPE,
    PENDING,
    RUNNING,
    agent_job_runtime_metadata,
    claim_next_agent_job,
    create_agent_job,
    deserialize_agent_job_payload,
    mark_agent_job_done,
    mark_agent_job_failed,
    reset_running_agent_jobs,
)
from system_app.services.clock_service import ensure_clock
from system_app.services.daily_pattern_scheduler import (
    ensure_due_daily_pattern_job,
)
from system_app.services.dose_event_service import ensure_day_events, prepare_notification_window
from system_app.services.failure_copy import copy_for_async_task
from system_app.services.patient_profile_service import can_run_simulation, ensure_base_data

logger = logging.getLogger("uvicorn.error")
def clock_worker(stop_event: threading.Event, write_lock: threading.RLock) -> None:
    while not stop_event.wait(1):
        try:
            with write_lock:
                with SessionLocal() as session:
                    clock = ensure_clock(session)
                    if not clock.is_running or clock.speed_multiplier <= 0:
                        continue
                    if not can_run_simulation(session):
                        clock.is_running = False
                        clock.speed_multiplier = 0
                        clock.last_tick_real_at = utc_now()
                        session.commit()
                        continue
                    clock.current_time = clock.current_time + timedelta(minutes=clock.speed_multiplier)
                    ensure_day_events(session, clock.current_time.date())
                    clock.last_tick_real_at = utc_now()
                    session.commit()
        except Exception as exc:  # pragma: no cover - defensive path
            logger.error("clock_worker_loop_failed error=%s", safe_exception_summary(exc))
            stop_event.wait(1)


def mark_awaiting_conversation_alert_failed(
    session,
    source_event_type: str,
    related_dose_event_id: int | None,
    job_id: int,
    error: Exception,
) -> None:
    if source_event_type != "missed_dose" or related_dose_event_id is None:
        return
    notification = session.scalar(
        select(Notification)
        .where(
            Notification.notification_type == "conversation_alert",
            Notification.related_dose_event_id == related_dose_event_id,
            Notification.acknowledged.is_(False),
        )
        .order_by(desc(Notification.created_at), desc(Notification.id))
    )
    if notification is None:
        return
    metadata = parse_metadata_json(notification.metadata_json)
    if metadata.get("status") != "awaiting_agent":
        return
    metadata.update(
        {
            "status": "agent_error",
            "agent_job_id": job_id,
            "error_type": getattr(error, "error_type", "agent_runtime_error"),
        }
    )
    metadata.pop("trace_id", None)
    notification.body = copy_for_async_task("missed_dose").body
    notification.metadata_json = dump_metadata_json(metadata)
    session.flush()


def persist_agent_failure(job_id: int, source_event_type: str, payload: object, error: Exception, write_lock: threading.RLock) -> None:
    with write_lock:
        with SessionLocal() as session:
            job = session.get(AgentJob, job_id)
            if job is None or job.status != RUNNING:
                logger.debug("agent_job_failure_discarded job_id=%s job_type=%s reason=stale_or_missing", job_id, source_event_type)
                return
            safe_error = safe_exception_summary(error)
            max_attempts = max(1, get_settings().agent_task_max_attempts)
            if (
                bool(getattr(error, "retryable", False))
                and job.attempts < max_attempts
            ):
                # A timeout/transport exception can happen after AI Server
                # durably accepted the request. Keep the Backend job
                # non-terminal and resend the same request_id; AI Server's
                # request-id idempotency then returns duplicate or accepts it
                # exactly once.
                job.status = PENDING
                job.started_at = None
                job.completed_at = None
                job.updated_at = utc_now()
                job.error_message = safe_error
                session.commit()
                logger.warning(
                    "agent_job_delivery_retry_scheduled "
                    "job_id=%s job_type=%s attempt=%s max_attempts=%s "
                    "error=%s",
                    job_id,
                    source_event_type,
                    job.attempts,
                    max_attempts,
                    safe_error,
                )
                return
            mark_agent_job_failed(session, job_id, safe_error)
            job = session.get(AgentJob, job_id)
            if source_event_type == DAILY_PATTERN_JOB_TYPE:
                session.commit()
                logger.error(
                    "agent_job_failed job_id=%s job_type=%s error=%s",
                    job_id,
                    source_event_type,
                    safe_error,
                )
                return
            related_dose_event_id = getattr(payload, "dose_event_id", None)
            mark_awaiting_conversation_alert_failed(session, source_event_type, related_dose_event_id, job_id, error)
            present_agent_error(
                session,
                source_event_type,
                error,
                user_message=copy_for_async_task(source_event_type).body,
                related_dose_event_id=related_dose_event_id,
                metadata=agent_job_runtime_metadata(job),
            )
            session.commit()
            logger.error("agent_job_failed job_id=%s job_type=%s error=%s", job_id, source_event_type, safe_error)


def fail_agent_job_before_send(session, job: AgentJob, error: Exception) -> None:
    mark_agent_job_failed(session, job.id, safe_exception_summary(error))
    if job.job_type == DAILY_PATTERN_JOB_TYPE:
        session.flush()
        return
    mark_awaiting_conversation_alert_failed(session, job.job_type, job.related_dose_event_id, job.id, error)
    present_agent_error(
        session,
        job.job_type,
        error,
        user_message="백그라운드 AI 작업 payload를 해석하지 못했습니다. 작업을 다시 생성해주세요.",
        related_dose_event_id=job.related_dose_event_id,
        metadata=agent_job_runtime_metadata(job),
    )
    session.flush()


def agent_worker(stop_event: threading.Event, write_lock: threading.RLock, agent_client: AgentClient) -> None:
    while not stop_event.is_set():
        try:
            with write_lock:
                with SessionLocal() as session:
                    clock = ensure_clock(session)
                    ensure_due_daily_pattern_job(
                        session,
                        current_time=clock.current_time,
                    )
                    job = claim_next_agent_job(session)
                    if job is None:
                        session.commit()
                        job_snapshot = None
                    else:
                        job_id = job.id
                        job_type = job.job_type
                        try:
                            payload = deserialize_agent_job_payload(job)
                            request = external_agent_request(
                                session,
                                job,
                                payload,
                            )
                        except Exception as exc:
                            logger.error("agent_job_payload_invalid job_id=%s job_type=%s error=%s", job_id, job_type, safe_exception_summary(exc))
                            fail_agent_job_before_send(session, job, exc)
                            session.commit()
                            continue
                        job_snapshot = (
                            job_id,
                            job_type,
                            payload,
                            request,
                        )
                        session.commit()
        except Exception as exc:  # pragma: no cover - defensive path
            logger.error("agent_worker_loop_failed error=%s", safe_exception_summary(exc))
            stop_event.wait(1)
            continue

        if job_snapshot is None:
            stop_event.wait(0.5)
            continue

        job_id, job_type, payload, request = job_snapshot
        try:
            if job_type == DAILY_PATTERN_JOB_TYPE:
                asyncio.run(
                    agent_client.send_daily_pattern_analysis_async(request)
                )
                with write_lock:
                    with SessionLocal() as session:
                        mark_agent_job_done(session, job_id)
                        session.commit()
            else:
                asyncio.run(
                    agent_client.send_medication_event_async(request)
                )
        except Exception as exc:  # pragma: no cover - defensive path
            persist_agent_failure(job_id, job_type, payload, exc, write_lock)


def external_agent_request(
    session,
    job: AgentJob,
    payload: MissedDoseEventPayload
    | DailyMedicationPatternAnalysisRequest,
) -> MissedDoseEventRequest | DailyMedicationPatternAnalysisRequest:
    """Map Backend-owned job state to the exact external v1.3 request DTO."""

    if job.job_type == DAILY_PATTERN_JOB_TYPE and isinstance(
        payload,
        DailyMedicationPatternAnalysisRequest,
    ):
        if payload.request_id != job.request_id:
            raise ValueError("daily_pattern_request_id_mismatch")
        return payload
    if job.job_type != "missed_dose" or not isinstance(
        payload,
        MissedDoseEventPayload,
    ):
        raise ValueError("unsupported_external_agent_job_type")
    dose_event_id = _public_dose_event_id(
        session,
        payload.dose_event_id,
        required=True,
    )
    return MissedDoseEventRequest(
        request_id=job.request_id,
        patient_id=payload.patient_id,
        dose_event_id=dose_event_id,
    )


def _public_dose_event_id(
    session,
    value: str | int | None,
    *,
    required: bool,
) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        row = session.get(DoseEvent, value)
        if row is not None and row.public_id:
            return row.public_id
    if required:
        raise ValueError("dose_event_public_id_not_found")
    return None


def notification_worker(stop_event: threading.Event, write_lock: threading.RLock) -> None:
    while not stop_event.wait(0.25):
        try:
            missed_payloads = []
            with write_lock:
                with SessionLocal() as session:
                    clock = ensure_clock(session)
                    start_dt = clock.last_processed_sim_time
                    end_dt = clock.current_time
                    if end_dt > start_dt:
                        missed_payloads = prepare_notification_window(
                            session,
                            start_dt,
                            end_dt,
                        )

            for payload in missed_payloads:
                with write_lock:
                    with SessionLocal() as session:
                        create_agent_job(session, "missed_dose", payload)
                        session.commit()

        except Exception as exc:  # pragma: no cover - defensive path
            logger.error("notification_worker_loop_failed error=%s", safe_exception_summary(exc))
            stop_event.wait(1)


def initialize_runtime_state(write_lock: threading.RLock) -> None:
    with write_lock:
        with SessionLocal() as session:
            reset_running_agent_jobs(session)
            ensure_base_data(session)
            session.commit()
