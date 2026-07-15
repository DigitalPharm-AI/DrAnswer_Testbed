from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from typing import Any

from sqlalchemy import desc, select

from shared.json_utils import dump_json as dump_metadata_json
from shared.json_utils import parse_json_object as parse_metadata_json
from shared.redaction import safe_exception_summary
from shared.schemas import AgentCallbackContext
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.db import SessionLocal
from system_app.models import AgentJob, Base, Notification
from system_app.services import trace_logging
from system_app.services.agent_client import AgentClient
from system_app.services.agent_error_service import present_agent_error
from system_app.services.agent_jobs import (
    RUNNING,
    agent_job_runtime_metadata,
    claim_next_agent_job,
    create_agent_job,
    deserialize_agent_job_payload,
    mark_agent_job_failed,
    reset_running_agent_jobs,
)
from system_app.services.clock_service import ensure_clock
from system_app.services.dose_event_service import ensure_day_events, prepare_notification_window
from system_app.services.failure_copy import copy_for_async_task
from system_app.services.patient_profile_service import can_run_simulation, ensure_base_data
from system_app.services.system_request_service import (
    apply_async_continuation_ack,
    apply_system_event_response,
    build_async_continuation_request,
    build_multiturn_chat_request,
    mark_system_event_async_submitted,
    mark_system_event_request_failed,
    response_requires_async_continuation,
)

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


def attach_job_callback_context(payload: Any, job_id: int) -> Any:
    callback_context = getattr(payload, "callback_context", None)
    if callback_context is None:
        callback_context = AgentCallbackContext(app_base_url=get_settings().system_base_url, job_id=job_id)
    callback_context.job_id = job_id
    payload.callback_context = callback_context
    return payload

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
            "trace_id": getattr(error, "trace_id", metadata.get("trace_id")),
        }
    )
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
            mark_agent_job_failed(session, job_id, safe_error)
            job = session.get(AgentJob, job_id)
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
                    job = claim_next_agent_job(session)
                    if job is None:
                        session.commit()
                        job_snapshot = None
                    else:
                        job_id = job.id
                        job_type = job.job_type
                        try:
                            payload = deserialize_agent_job_payload(job)
                        except Exception as exc:
                            logger.error("agent_job_payload_invalid job_id=%s job_type=%s error=%s", job_id, job_type, safe_exception_summary(exc))
                            fail_agent_job_before_send(session, job, exc)
                            session.commit()
                            continue
                        job_snapshot = (job_id, job_type, payload)
                        session.commit()
        except Exception as exc:  # pragma: no cover - defensive path
            logger.error("agent_worker_loop_failed error=%s", safe_exception_summary(exc))
            stop_event.wait(1)
            continue

        if job_snapshot is None:
            stop_event.wait(0.5)
            continue

        job_id, job_type, payload = job_snapshot
        try:
            if job_type == "missed_dose":
                payload = attach_job_callback_context(payload, job_id)
                asyncio.run(agent_client.send_missed_dose_async(payload))
            elif job_type == "daily_pattern":
                payload = attach_job_callback_context(payload, job_id)
                asyncio.run(agent_client.send_daily_pattern_async(payload))
        except Exception as exc:  # pragma: no cover - defensive path
            persist_agent_failure(job_id, job_type, payload, exc, write_lock)


def system_event_worker(
    event_type: str,
    message: str,
    notification_id: int,
    write_lock: threading.RLock,
    agent_client: AgentClient,
) -> None:
    trace_logging.log_info(
        "system_event_worker_started",
        event_type=event_type,
        notification_id=notification_id,
        message=trace_logging.snippet(message),
    )
    try:
        with write_lock:
            with SessionLocal() as session:
                request = build_multiturn_chat_request(session, event_type, message, notification_id)
                session.commit()

        trace_logging.log_info(
            "system_event_multiturn_chat_agent_call_started",
            event_type=event_type,
            notification_id=notification_id,
            phr_registered=bool(request.phr_patient_key),
            context_keys=sorted(request.context.keys()),
        )
        if hasattr(agent_client, "send_chat_continuation_async"):
            accepted = asyncio.run(agent_client.send_chat_continuation_async(request))
            with write_lock:
                with SessionLocal() as session:
                    mark_system_event_async_submitted(
                        session,
                        event_type,
                        message,
                        notification_id,
                        request_id=getattr(accepted, "request_id", ""),
                        task_type=getattr(accepted, "task_type", "chat_continuation"),
                    )
                    session.commit()
            trace_logging.log_info(
                "system_event_async_chat_submitted",
                event_type=event_type,
                notification_id=notification_id,
                request_id=getattr(accepted, "request_id", ""),
                task_type=getattr(accepted, "task_type", "chat_continuation"),
            )
            return

        response = asyncio.run(agent_client.send_multiturn_chat(request))
        trace_logging.log_info(
            "system_event_multiturn_chat_agent_call_completed",
            event_type=event_type,
            notification_id=notification_id,
            trace_id=response.trace_id,
            agent=response.agent_name,
            decision=response.decision_type,
            summary=trace_logging.snippet(response.human_summary),
        )

        if response_requires_async_continuation(response):
            continuation_request = build_async_continuation_request(request, response)
            with write_lock:
                with SessionLocal() as session:
                    apply_async_continuation_ack(session, event_type, message, notification_id, response)
                    session.commit()
            if hasattr(agent_client, "send_chat_continuation_async"):
                asyncio.run(agent_client.send_chat_continuation_async(continuation_request))
            else:
                continuation_response = asyncio.run(agent_client.send_multiturn_chat(continuation_request))
                with write_lock:
                    with SessionLocal() as session:
                        apply_system_event_response(session, event_type, message, notification_id, continuation_response)
                        session.commit()
            trace_logging.log_info(
                "system_event_async_continuation_submitted",
                event_type=event_type,
                notification_id=notification_id,
                trace_id=response.trace_id,
                continuation_type=response.structured_payload.get("async_continuation_type"),
            )
            return

        with write_lock:
            with SessionLocal() as session:
                apply_system_event_response(session, event_type, message, notification_id, response)
                session.commit()
        trace_logging.log_info(
            "system_event_response_persisted",
            event_type=event_type,
            notification_id=notification_id,
            trace_id=response.trace_id,
            decision=response.decision_type,
        )
    except Exception as exc:  # pragma: no cover - defensive path
        safe_error = safe_exception_summary(exc)
        logger.error("system_event_worker_failed notification_id=%s event_type=%s error=%s", notification_id, event_type, safe_error)
        trace_logging.log_warning(
            "system_event_worker_failed",
            event_type=event_type,
            notification_id=notification_id,
            error_type=type(exc).__name__,
            error=safe_error,
        )
        with write_lock:
            with SessionLocal() as session:
                mark_system_event_request_failed(session, event_type, message, notification_id, exc)
                session.commit()


def notification_worker(stop_event: threading.Event, write_lock: threading.RLock) -> None:
    while not stop_event.wait(0.25):
        try:
            missed_payloads = []
            pattern_jobs = []
            with write_lock:
                with SessionLocal() as session:
                    clock = ensure_clock(session)
                    start_dt = clock.last_processed_sim_time
                    end_dt = clock.current_time
                    if end_dt > start_dt:
                        missed_payloads, pattern_jobs = prepare_notification_window(session, start_dt, end_dt)

            for payload in missed_payloads:
                with write_lock:
                    with SessionLocal() as session:
                        create_agent_job(session, "missed_dose", payload)
                        session.commit()

            for pattern in pattern_jobs:
                with write_lock:
                    with SessionLocal() as session:
                        create_agent_job(session, "daily_pattern", pattern)
                        session.commit()
        except Exception as exc:  # pragma: no cover - defensive path
            logger.error("notification_worker_loop_failed error=%s", safe_exception_summary(exc))
            stop_event.wait(1)


def initialize_runtime_state(write_lock: threading.RLock) -> None:
    from system_app.db import engine

    Base.metadata.create_all(bind=engine)
    with write_lock:
        with SessionLocal() as session:
            reset_running_agent_jobs(session)
            ensure_base_data(session)
            session.commit()
