from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.async_v13_contracts import (
    AsyncResultCallbackAck,
    MissedDoseResultCallback,
    NotificationPolicyChangeProposalRequest,
    NotificationPolicyProposalAck,
)
from shared.backend_v13_contracts import ContractError
from shared.json_utils import dump_json, parse_json_object
from shared.schemas import (
    AgentAsyncClinicianAlertRequest,
    AgentAsyncPushMessageRequest,
    AgentNotificationRequest,
    AgentResponse,
)
from shared.time_utils import utc_now
from system_app.models import (
    AgentAsyncCallbackReceipt,
    AgentJob,
    DoseEvent,
    Notification,
    NotificationPolicyChangeProposal,
    ReminderPolicy,
)
from system_app.services.adherence_pattern_service import CLINICIAN_ESCALATION_CATEGORY, CLINICIAN_ESCALATION_TYPE
from system_app.services.agent_callback_service import process_agent_notification_callback
from system_app.services.agent_client import AgentServiceError
from system_app.services.agent_error_service import present_agent_error
from system_app.services.agent_jobs import (
    PENDING,
    RUNNING,
    mark_agent_job_done,
    mark_agent_job_failed,
)
from system_app.services.agent_response_service import persist_agent_summary
from system_app.services.backend_v13_service import BackendRequestGate
from system_app.services.clock_service import ensure_clock
from system_app.services.failure_copy import copy_for_async_task
from system_app.services.notification_service import create_notification
from system_app.services.timeline_service import add_chat_message
from system_app.services.workers import mark_awaiting_conversation_alert_failed


class AsyncCallbackContractError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = ContractError(
            code=code,
            message=message,
            retryable=retryable,
            details=details,
        )


def process_missed_dose_result_callback(
    session: Session,
    payload: MissedDoseResultCallback,
) -> AsyncResultCallbackAck:
    callback_hash = _minimal_callback_body_hash(payload)
    existing_receipt = session.get(
        AgentAsyncCallbackReceipt,
        payload.request_id,
    )
    if existing_receipt is not None:
        return _resolve_missed_dose_receipt(
            existing_receipt,
            callback_hash,
        )

    job = session.scalar(
        select(AgentJob).where(AgentJob.request_id == payload.request_id)
    )
    if job is None:
        raise _callback_error(
            404,
            "ASYNC_REQUEST_NOT_FOUND",
            "The original asynchronous request was not found.",
            details={"request_id": payload.request_id},
        )
    if job.job_type != "missed_dose" or job.status not in {PENDING, RUNNING}:
        raise _callback_error(
            409,
            "CALLBACK_STATE_CONFLICT",
            "The asynchronous request is already in a conflicting terminal state.",
            details={"request_id": payload.request_id},
        )
    if job.related_dose_event_id is None:
        raise _callback_error(
            409,
            "CALLBACK_STATE_CONFLICT",
            "The asynchronous request is already in a conflicting terminal state.",
            details={"request_id": payload.request_id},
        )
    event = session.scalar(
        select(DoseEvent)
        .where(DoseEvent.id == job.related_dose_event_id)
        .with_for_update()
    )
    if event is None:
        raise _callback_error(
            409,
            "CALLBACK_STATE_CONFLICT",
            "The asynchronous request is already in a conflicting terminal state.",
            details={"request_id": payload.request_id},
        )

    receipt = AgentAsyncCallbackReceipt(
        request_id=payload.request_id,
        callback_hash=callback_hash,
        event_type="missed_dose",
        result_status=payload.status,
        processed_at=utc_now(),
    )
    session.add(receipt)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        winner = session.get(
            AgentAsyncCallbackReceipt,
            payload.request_id,
        )
        if winner is None:
            raise
        return _resolve_missed_dose_receipt(winner, callback_hash)

    if payload.status == "completed":
        if payload.result is None:
            raise ValueError("completed_callback_result_missing")
        if event.status != "missed":
            _suppress_stale_missed_dose_alert(
                session,
                event=event,
                request_id=payload.request_id,
            )
            mark_agent_job_done(session, job.id)
            session.commit()
            return AsyncResultCallbackAck(
                request_id=payload.request_id,
                status="processed",
            )
        persist_agent_summary(
            session,
            AgentResponse(
                trace_id="",
                agent_name="",
                prompt_version_id="",
                decision_type="missed_dose_result",
                structured_payload={
                    "missed_dose_hybrid": {
                        "generated_message": payload.result.message,
                    }
                },
                human_summary=payload.result.message,
                requires_conversation_alert=payload.result.requires_reply,
            ),
            category="missed_dose",
            related_dose_event_id=event.id,
        )
        mark_agent_job_done(session, job.id)
    else:
        if payload.error is None:
            raise ValueError("failed_callback_error_missing")
        error = AgentServiceError(
            payload.error.message,
            status_code=None,
            error_type=payload.error.code,
            retryable=payload.error.retryable,
            details=None,
        )
        mark_agent_job_failed(session, job.id, payload.error.message)
        mark_awaiting_conversation_alert_failed(
            session,
            "missed_dose",
            event.id,
            job.id,
            error,
        )
        present_agent_error(
            session,
            "missed_dose",
            error,
            user_message=copy_for_async_task("missed_dose").body,
            related_dose_event_id=event.id,
            metadata={
                "agent_job_id": job.id,
                "request_id": payload.request_id,
            },
        )

    session.commit()
    return AsyncResultCallbackAck(
        request_id=payload.request_id,
        status="processed",
    )


def _suppress_stale_missed_dose_alert(
    session: Session,
    *,
    event: DoseEvent,
    request_id: str,
) -> None:
    resolved_at = utc_now()
    rows = session.scalars(
        select(Notification).where(
            Notification.notification_type == "conversation_alert",
            Notification.related_dose_event_id == event.id,
            Notification.acknowledged.is_(False),
        )
    ).all()
    for notification in rows:
        metadata = parse_json_object(notification.metadata_json)
        if metadata.get("category") != "missed_dose":
            continue
        metadata.update(
            {
                "status": "superseded",
                "superseded_reason": (
                    f"dose_status_changed_to_{event.status}"
                ),
                "resolved_at": resolved_at.isoformat(),
                "request_id": request_id,
            }
        )
        notification.metadata_json = dump_json(metadata)
        notification.acknowledged = True
    session.flush()


def process_notification_policy_change_proposal(
    session: Session,
    payload: NotificationPolicyChangeProposalRequest,
) -> tuple[NotificationPolicyProposalAck, bool]:
    callback_hash = _minimal_callback_body_hash(payload)
    existing = session.get(
        NotificationPolicyChangeProposal,
        payload.request_id,
    )
    if existing is not None:
        return (
            _resolve_policy_proposal(existing, callback_hash),
            False,
        )

    clock = ensure_clock(session)
    target_date = clock.current_time.date()
    active_policy = session.scalar(
        select(ReminderPolicy.id).where(
            ReminderPolicy.patient_id == payload.patient_id,
            ReminderPolicy.active.is_(True),
            ReminderPolicy.effective_start_date <= target_date,
            ReminderPolicy.effective_end_date >= target_date,
        ).limit(1)
    )
    if active_policy is None:
        raise _callback_error(
            422,
            "INVALID_POLICY_PROPOSAL",
            "The notification policy proposal is invalid.",
            details={
                "patient_id": payload.patient_id,
                "reason": "active_notification_policy_not_found",
            },
        )

    proposal = NotificationPolicyChangeProposal(
        request_id=payload.request_id,
        callback_hash=callback_hash,
        patient_id=payload.patient_id,
        proposed_policy_json=dump_json(
            payload.proposed_policy.model_dump(mode="json")
        ),
        reason=payload.reason,
        status="pending_user_confirmation",
    )
    try:
        with session.begin_nested():
            session.add(proposal)
            session.flush()
    except IntegrityError:
        winner = session.get(
            NotificationPolicyChangeProposal,
            payload.request_id,
        )
        if winner is None:
            raise
        return (
            _resolve_policy_proposal(winner, callback_hash),
            False,
        )

    proposed_policy = payload.proposed_policy.model_dump(
        mode="json",
        exclude_none=True,
    )
    message = (
        f"{payload.reason}\n"
        "알림 정책 변경 제안을 확인해 주세요. "
        "수락 후 대화에서 적용 대상을 확인합니다."
    )
    notification = create_notification(
        session,
        notification_type="conversation_alert",
        title="복약 알림 정책 변경 제안",
        body=message,
        visible_at=clock.current_time,
        patient_id=payload.patient_id,
        metadata={
            "category": "policy_confirmation",
            "status": "agent_ready",
            "source_event_type": "daily_pattern_policy_proposal",
            "proposal_request_id": payload.request_id,
            "proposed_policy": proposed_policy,
            "reason": payload.reason,
            "target_scope": "patient",
            "policy_applied": False,
        },
    )
    add_chat_message(
        session,
        role="assistant",
        content=message,
        sender_type="assistant",
        category="policy_confirmation",
        patient_id=payload.patient_id,
        ai_request_id=payload.request_id,
        metadata={
            "conversation_alert": {
                "notification_id": notification.public_id,
                "category": "policy_confirmation",
                "status": "agent_ready",
                "reply_mode": "chat",
            },
            "policy_confirmation": {
                "proposal_request_id": payload.request_id,
                "proposed_policy": proposed_policy,
                "reason": payload.reason,
                "target_scope": "patient",
                "policy_applied": False,
            },
        },
    )
    proposal.notification_id = notification.id
    session.commit()
    return (
        NotificationPolicyProposalAck(
            request_id=payload.request_id,
            status="accepted",
        ),
        True,
    )


def _resolve_missed_dose_receipt(
    receipt: AgentAsyncCallbackReceipt,
    callback_hash: str,
) -> AsyncResultCallbackAck:
    if (
        receipt.event_type == "missed_dose"
        and receipt.callback_hash == callback_hash
    ):
        return AsyncResultCallbackAck(
            request_id=receipt.request_id,
            status="duplicate",
        )
    raise _callback_error(
        409,
        "CALLBACK_STATE_CONFLICT",
        "The asynchronous request is already in a conflicting terminal state.",
        details={"request_id": receipt.request_id},
    )


def _resolve_policy_proposal(
    proposal: NotificationPolicyChangeProposal,
    callback_hash: str,
) -> NotificationPolicyProposalAck:
    if proposal.callback_hash == callback_hash:
        return NotificationPolicyProposalAck(
            request_id=proposal.request_id,
            status="duplicate",
        )
    raise _callback_error(
        409,
        "IDEMPOTENCY_CONFLICT",
        "The request_id was reused with a different request body.",
        details={"request_id": proposal.request_id},
    )


def _minimal_callback_body_hash(
    payload: MissedDoseResultCallback
    | NotificationPolicyChangeProposalRequest,
) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _callback_error(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict | None = None,
) -> AsyncCallbackContractError:
    return AsyncCallbackContractError(
        status_code=status_code,
        code=code,
        message=message,
        retryable=status_code >= 500,
        details=details,
    )

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
    request_gate = BackendRequestGate()
    replay = request_gate.begin(
        session,
        api_path="/agent/async/clinician-alerts",
        request_id=idempotency_key,
        payload=payload,
    )
    if replay is not None:
        result = dict(replay.body)
        result["status"] = "duplicate"
        return result

    existing = _existing_clinician_alert(session, payload.related_dose_event_id, payload.pattern_code)
    if existing is not None:
        result = {
            "status": "duplicate",
            "request_id": payload.request_id,
            "notification_id": existing.id,
        }
        request_gate.complete(
            session,
            api_path="/agent/async/clinician-alerts",
            request_id=idempotency_key,
            status_code=200,
            body=result,
        )
        session.commit()
        return result

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
    result = {
        "status": "ok",
        "request_id": payload.request_id,
        "notification_id": notification.id,
    }
    request_gate.complete(
        session,
        api_path="/agent/async/clinician-alerts",
        request_id=idempotency_key,
        status_code=200,
        body=result,
    )
    session.commit()
    return result


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
