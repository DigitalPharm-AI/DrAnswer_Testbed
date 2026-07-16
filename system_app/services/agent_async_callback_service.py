from __future__ import annotations

from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.schemas import (
    AgentAsyncChatResultRequest,
    AgentAsyncClinicianAlertRequest,
    AgentAsyncFailureRequest,
    AgentAsyncJobResultRequest,
    AgentAsyncPolicyChangeRequest,
    AgentAsyncPushMessageRequest,
    AgentNotificationRequest,
)
from system_app.models import AgentDecisionAudit, AgentJob, Notification
from system_app.services.adherence_pattern_service import CLINICIAN_ESCALATION_CATEGORY, CLINICIAN_ESCALATION_TYPE
from system_app.services.agent_callback_service import process_agent_notification_callback
from system_app.services.agent_client import AgentServiceError
from system_app.services.agent_error_service import present_agent_error
from system_app.services.agent_jobs import PENDING, RUNNING, deserialize_agent_job_payload, mark_agent_job_done, mark_agent_job_failed
from system_app.services.agent_response_service import maybe_apply_policy_response, persist_agent_summary, tool_results_have_error
from system_app.services.agent_trace_store import record_agent_run_failure, upsert_agent_run_trace
from system_app.services.audit_service import create_agent_decision_audit, record_agent_audit
from system_app.services.clock_service import ensure_clock
from system_app.services.failure_copy import copy_for_async_task
from system_app.services.notification_service import create_notification
from system_app.services.system_request_service import apply_system_event_response, mark_system_event_request_failed
from system_app.services.workers import mark_awaiting_conversation_alert_failed


class AgentAsyncCallbackError(AgentServiceError):
    def __init__(self, payload: AgentAsyncFailureRequest) -> None:
        super().__init__(
            payload.message,
            error_type=payload.error_type,
            trace_id=payload.trace_id,
            agent_name=payload.agent_name,
            decision_type=payload.decision_type,
        )


def process_async_job_result_callback(session: Session, payload: AgentAsyncJobResultRequest) -> dict:
    if _is_duplicate(session, payload.idempotency_key, "agent_async_job_result"):
        return {"status": "duplicate", "request_id": payload.request_id}
    job = _resolve_job(session, payload.job_id, payload.request_id)
    if job is None:
        return {"status": "not_found", "request_id": payload.request_id}
    if job.status not in {PENDING, RUNNING}:
        return {"status": "stale", "request_id": payload.request_id, "job_id": job.id, "job_status": job.status}

    if payload.task_type == "missed_dose":
        source_payload = deserialize_agent_job_payload(job)
        persist_agent_summary(session, payload.response, category="missed_dose", related_dose_event_id=getattr(source_payload, "dose_event_id", None))
        record_agent_audit(session, payload.response, "missed_dose", applied=False, error_message="")
        mark_agent_job_done(session, job.id)
    else:
        persist_agent_summary(session, payload.response, category="pattern_analysis")
        _applied, result_message = maybe_apply_policy_response(session, payload.response, "daily_pattern")
        if tool_results_have_error(payload.response):
            failure_copy = copy_for_async_task("daily_pattern")
            mark_agent_job_failed(session, job.id, result_message or payload.response.human_summary or "일일 패턴 분석 결과를 적용하지 못했습니다.")
            present_agent_error(
                session,
                "daily_pattern",
                RuntimeError(result_message or payload.response.human_summary or "agent tool failed"),
                user_message=failure_copy.body,
                metadata={"agent_job_id": job.id, "trace_id": payload.response.trace_id},
            )
        else:
            mark_agent_job_done(session, job.id)
    upsert_agent_run_trace(
        session,
        payload.response,
        workflow_name=payload.task_type,
        source_event_type="agent_async_job_result",
        status="completed",
        request_id=payload.request_id,
        job_id=job.id,
        related_dose_event_id=payload.related_dose_event_id,
    )
    _record_idempotency(session, payload.idempotency_key, "agent_async_job_result", {"job_id": job.id}, payload.response.human_summary)
    session.commit()
    return {"status": "ok", "request_id": payload.request_id, "job_id": job.id}


def process_async_chat_result_callback(session: Session, payload: AgentAsyncChatResultRequest) -> dict:
    if _is_duplicate(session, payload.idempotency_key, "agent_async_chat_result"):
        return {"status": "duplicate", "request_id": payload.request_id}
    follow_up: dict | None = None
    if payload.notification_id is not None:
        follow_up = apply_system_event_response(
            session,
            payload.event_type,
            payload.message,
            payload.notification_id,
            payload.response,
        )
        _mark_async_continuation_status(session, payload.notification_id, "done")
    else:
        persist_agent_summary(session, payload.response, category="multiturn_chat")
        upsert_agent_run_trace(
            session,
            payload.response,
            workflow_name=payload.event_type,
            source_event_type="agent_async_chat_result",
            status="completed",
            request_id=payload.request_id,
            request_message=payload.message,
        )
    _record_idempotency(session, payload.idempotency_key, "agent_async_chat_result", {"notification_id": payload.notification_id}, payload.response.human_summary)
    session.commit()
    result = {"status": "ok", "request_id": payload.request_id, "notification_id": payload.notification_id}
    if follow_up:
        result.update(follow_up)
    return result


def process_async_policy_change_callback(session: Session, payload: AgentAsyncPolicyChangeRequest) -> dict:
    if _is_duplicate(session, payload.idempotency_key, "agent_async_policy_change"):
        return {"status": "duplicate", "request_id": payload.request_id}
    if payload.notification_id is not None:
        notification = session.get(Notification, payload.notification_id)
        metadata = parse_json_object(notification.metadata_json) if notification is not None else {}
        request_message = str(metadata.get("request_message") or "")
        apply_system_event_response(session, payload.source_event_type, request_message, payload.notification_id, payload.response)
        _mark_async_continuation_status(session, payload.notification_id, "done")
    else:
        maybe_apply_policy_response(session, payload.response, payload.source_event_type)
        upsert_agent_run_trace(
            session,
            payload.response,
            workflow_name=payload.source_event_type,
            source_event_type="agent_async_policy_change",
            status="completed",
            request_id=payload.request_id,
        )
    _record_idempotency(session, payload.idempotency_key, "agent_async_policy_change", {"notification_id": payload.notification_id}, payload.response.human_summary)
    session.commit()
    return {"status": "ok", "request_id": payload.request_id, "notification_id": payload.notification_id}


def process_async_push_message_callback(session: Session, payload: AgentAsyncPushMessageRequest) -> dict:
    idempotency_key = payload.idempotency_key or payload.request_id
    callback = AgentNotificationRequest(
        title=payload.title,
        body=payload.message,
        notification_type=payload.type,
        related_dose_event_id=payload.related_dose_event_id,
        metadata=payload.metadata,
        visible_at=payload.send_time,
        chat_category=payload.chat_category,
        idempotency_key=idempotency_key,
    )
    result = process_agent_notification_callback(session, callback)
    result["request_id"] = payload.request_id
    return result


def process_async_clinician_alert_callback(session: Session, payload: AgentAsyncClinicianAlertRequest) -> dict:
    idempotency_key = payload.idempotency_key or payload.request_id
    if _is_duplicate(session, idempotency_key, "agent_async_clinician_alert"):
        return {"status": "duplicate", "request_id": payload.request_id}

    existing = _existing_clinician_alert(session, payload.related_dose_event_id, payload.pattern_code)
    if existing is not None:
        _record_idempotency(
            session,
            idempotency_key,
            "agent_async_clinician_alert",
            {"notification_id": existing.id, "deduped_by": "dose_event_pattern"},
            payload.message,
        )
        session.commit()
        return {"status": "duplicate", "request_id": payload.request_id, "notification_id": existing.id}

    clock = ensure_clock(session)
    metadata = {
        **payload.metadata,
        "source": "agent_async_clinician_alert",
        "request_id": payload.request_id,
        "priority": payload.priority,
    }
    if payload.related_dose_event_id is not None:
        metadata["dose_event_id"] = payload.related_dose_event_id
    if payload.pattern_code:
        metadata["pattern_code"] = payload.pattern_code
    if payload.pattern_label:
        metadata["pattern_label"] = payload.pattern_label
    if payload.reason:
        metadata["reason"] = payload.reason
    if payload.streak_metrics:
        metadata["streak_metrics"] = payload.streak_metrics
    metadata["category"] = CLINICIAN_ESCALATION_CATEGORY
    metadata["delivery_channel"] = "internal_only"
    metadata["status"] = "stubbed"

    notification = create_notification(
        session,
        notification_type=CLINICIAN_ESCALATION_TYPE,
        title=payload.title,
        body=payload.message,
        visible_at=payload.visible_at or clock.current_time,
        related_dose_event_id=payload.related_dose_event_id,
        metadata=metadata,
    )
    _record_idempotency(
        session,
        idempotency_key,
        "agent_async_clinician_alert",
        {"notification_id": notification.id},
        payload.message,
    )
    session.commit()
    return {"status": "ok", "request_id": payload.request_id, "notification_id": notification.id}


def process_async_failure_callback(session: Session, payload: AgentAsyncFailureRequest) -> dict:
    if _is_duplicate(session, payload.idempotency_key, "agent_async_failure"):
        return {"status": "duplicate", "request_id": payload.request_id}
    job = _resolve_job(session, payload.job_id, payload.request_id)
    error = AgentAsyncCallbackError(payload)
    failure_copy = copy_for_async_task(payload.task_type, error_type=payload.error_type)
    if job is not None:
        mark_agent_job_failed(session, job.id, payload.message)
        mark_awaiting_conversation_alert_failed(session, payload.task_type, payload.related_dose_event_id or job.related_dose_event_id, job.id, error)
        present_agent_error(
            session,
            payload.task_type,
            error,
            user_message=failure_copy.body,
            related_dose_event_id=payload.related_dose_event_id or job.related_dose_event_id,
            metadata={"agent_job_id": job.id, "request_id": payload.request_id},
        )
    elif payload.notification_id is not None:
        mark_system_event_request_failed(session, "multiturn_chat", "", payload.notification_id, error)
        _mark_async_continuation_status(session, payload.notification_id, "failed")
    else:
        present_agent_error(
            session,
            payload.task_type,
            error,
            user_message=failure_copy.body,
            related_dose_event_id=payload.related_dose_event_id,
            metadata={"request_id": payload.request_id},
        )
    record_agent_run_failure(
        session,
        trace_id=payload.trace_id,
        workflow_name=payload.task_type,
        source_event_type="agent_async_failure",
        request_id=payload.request_id,
        agent_name=payload.agent_name,
        decision_type=payload.decision_type,
        error_message=payload.message,
        job_id=job.id if job is not None else payload.job_id,
        notification_id=payload.notification_id,
        related_dose_event_id=payload.related_dose_event_id,
    )
    _record_idempotency(session, payload.idempotency_key, "agent_async_failure", {"job_id": job.id if job is not None else None}, payload.message)
    session.commit()
    return {"status": "ok", "request_id": payload.request_id, "job_id": job.id if job is not None else None}


def _resolve_job(session: Session, job_id: int | None, request_id: str) -> AgentJob | None:
    if job_id is not None:
        return session.get(AgentJob, job_id)
    parts = request_id.split(":")
    if len(parts) >= 3 and parts[-2] == "job":
        try:
            return session.get(AgentJob, int(parts[-1]))
        except ValueError:
            return None
    return None


def _is_duplicate(session: Session, idempotency_key: str | None, source_event_type: str) -> bool:
    if not idempotency_key:
        return False
    existing = (
        session.query(AgentDecisionAudit)
        .filter(AgentDecisionAudit.trace_id == idempotency_key, AgentDecisionAudit.source_event_type == source_event_type)
        .first()
    )
    return existing is not None


def _record_idempotency(
    session: Session,
    idempotency_key: str | None,
    source_event_type: str,
    payload: dict,
    summary: str,
) -> None:
    if not idempotency_key:
        return
    create_agent_decision_audit(
        session,
        trace_id=idempotency_key,
        agent_name="agent_async_callback",
        prompt_version_id="n/a",
        decision_type=source_event_type,
        structured_payload=payload,
        human_summary=summary,
        applied=True,
        error_message="",
        source_event_type=source_event_type,
    )


def _existing_clinician_alert(
    session: Session,
    related_dose_event_id: int | None,
    pattern_code: str | None,
) -> Notification | None:
    if related_dose_event_id is None or not pattern_code:
        return None
    rows = (
        session.query(Notification)
        .filter(
            Notification.notification_type == CLINICIAN_ESCALATION_TYPE,
            Notification.related_dose_event_id == related_dose_event_id,
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(20)
        .all()
    )
    for row in rows:
        metadata = parse_json_object(row.metadata_json)
        if metadata.get("category") == CLINICIAN_ESCALATION_CATEGORY and metadata.get("pattern_code") == pattern_code:
            return row
    return None


def _mark_async_continuation_status(session: Session, notification_id: int, status: str) -> None:
    notification = session.get(Notification, notification_id)
    if notification is None:
        return
    metadata = parse_json_object(notification.metadata_json)
    metadata["async_continuation_status"] = status
    notification.metadata_json = dump_json(metadata)
    session.flush()
