from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.redaction import safe_exception_summary, safe_log_arguments
from shared.schemas import AgentCallbackContext, AgentResponse, MultiturnChatRequest, NotificationPolicyDelta
from shared.settings import get_settings
from system_app.models import Notification
from system_app.services import trace_logging
from system_app.services.agent_client import AgentClient
from system_app.services.agent_error_service import present_agent_error
from system_app.services.agent_response_service import maybe_apply_dose_taken_response, maybe_apply_policy_response, persist_agent_summary
from system_app.services.agent_trace_store import upsert_agent_run_trace
from system_app.services.clock_service import ensure_clock
from system_app.services.failure_copy import copy_for_agent_error
from system_app.services.medication_plan_service import get_schedule_slot_labels
from system_app.services.missed_dose_reply_understanding import (
    MISSED_DOSE_REPLY_METADATA_KEY,
    merge_missed_dose_reply_understanding_from_agent_response,
)
from system_app.services.notification_service import create_notification
from system_app.services.nutrition_service import build_nutrition_context
from system_app.services.patient_profile_service import get_phr_patient_key
from system_app.services.policy_confirmation import policy_deltas_from_tool_response
from system_app.services.policy_service import daily_pattern_conversation_time_view, resolve_policy_boundary_for_slot, resolve_policy_for_slot
from system_app.services.timeline_service import (
    add_chat_message,
    get_notifications,
    get_recent_chat_turns,
    get_recent_diet_recommendation_groups,
    get_today_dose_events,
)

settings = get_settings()
POLICY_CONFIRMATION_CHAT_MESSAGE = "정책 변경 후보를 채팅에 표시했어요. 아래 선택지에서 결정해주시면 그때 반영할게요."
POLICY_CONFIRMATION_CHAT_SUFFIX = "아래 선택지에서 결정해주시면 그때 반영할게요."


async def handle_system_event(session: Session, agent_client: AgentClient, event_type: str, message: str) -> None:
    clock = ensure_clock(session)
    request_notification = create_system_event_request(session, event_type, message, clock.current_time)
    session.commit()
    await complete_system_event_request(session, agent_client, event_type, message, request_notification.id)

def create_system_event_request(
    session: Session,
    event_type: str,
    message: str,
    current_time: datetime | None = None,
    metadata: dict | None = None,
) -> Notification:
    clock = ensure_clock(session)
    visible_at = current_time or clock.current_time
    request_metadata = dict(metadata or {})
    chat_message = add_chat_message(session, role="user", content=message, sender_type="patient", category=event_type, metadata=request_metadata)
    conversation_id = str(request_metadata.get("agent_conversation_id") or f"system-event-{chat_message.id}-{uuid4().hex[:12]}")
    request_metadata["agent_conversation_id"] = conversation_id
    notification = create_notification(
        session,
        notification_type="system_policy_request",
        title="에이전트 대화 전송",
        body=f"에이전트에게 메시지를 보냈습니다: {message}",
        visible_at=visible_at,
        metadata={
            "category": "agent_conversation",
            "status": "sent",
            "event_type": event_type,
            "request_message": message,
            "chat_message_id": chat_message.id,
            "agent_conversation_id": conversation_id,
            **request_metadata,
        },
    )
    trace_logging.log_info(
        "system_event_request_created",
        event_type=event_type,
        notification_id=notification.id,
        visible_at=visible_at.isoformat(),
        message=trace_logging.snippet(message),
    )
    return notification

def update_system_event_request_notification(
    session: Session,
    notification_id: int,
    *,
    status: str,
    request_message: str,
    result_message: str,
    response: AgentResponse | None = None,
) -> None:
    notification = session.get(Notification, notification_id)
    if notification is None:
        return
    metadata = parse_json_object(notification.metadata_json)
    metadata.update(
        {
            "status": status,
            "request_message": request_message,
            "result_message": result_message,
        }
    )
    if response is not None:
        metadata.update(
            {
                "trace_id": response.trace_id,
                "decision_type": response.decision_type,
                "agent_name": response.agent_name,
            }
        )
    notification.title = "에이전트 대화 완료" if status == "applied" else "에이전트 대화 전송"
    notification.body = f"요청이 반영되었습니다: {request_message}" if status == "applied" else f"에이전트에게 메시지를 보냈습니다: {request_message}"
    if status == "answered":
        notification.title = "에이전트 답변 완료"
        notification.body = result_message or f"에이전트가 답변했습니다: {request_message}"
    if status == "needs_clarification":
        notification.title = "에이전트 추가 확인 필요"
        notification.body = result_message or f"정책 적용 전에 확인이 필요합니다: {request_message}"
    if status == "needs_confirmation":
        notification.title = "정책 변경 확인 대기"
        notification.body = f"정책 적용 전에 환자 확인이 필요합니다: {request_message}"
    if status == "failed":
        notification.title = "에이전트 대화 처리 실패"
        notification.body = f"에이전트가 메시지를 처리하지 못했습니다: {request_message}"
    notification.metadata_json = dump_json(metadata)
    session.flush()
    trace_logging.log_info(
        "system_event_notification_updated",
        notification_id=notification_id,
        status=status,
        trace_id=response.trace_id if response is not None else None,
        decision=response.decision_type if response is not None else None,
        result=trace_logging.snippet(result_message),
    )

async def complete_system_event_request(
    session: Session,
    agent_client: AgentClient,
    event_type: str,
    message: str,
    request_notification_id: int,
) -> None:
    try:
        request = build_multiturn_chat_request(session, event_type, message, request_notification_id)
        response = await agent_client.send_multiturn_chat(request)
        if response_requires_async_continuation(response):
            apply_async_continuation_ack(session, event_type, message, request_notification_id, response)
            session.commit()
            await agent_client.send_chat_continuation_async(build_async_continuation_request(request, response))
            return
        apply_system_event_response(session, event_type, message, request_notification_id, response)
    except Exception as exc:  # pragma: no cover - network dependent
        mark_system_event_request_failed(session, event_type, message, request_notification_id, exc)
    session.commit()


def build_multiturn_chat_request(
    session: Session,
    event_type: str,
    message: str,
    request_notification_id: int,
) -> MultiturnChatRequest:
    clock = ensure_clock(session)
    schedule_slots = get_schedule_slot_labels(session)
    request_notification = session.get(Notification, request_notification_id)
    request_metadata = parse_json_object(request_notification.metadata_json) if request_notification is not None else {}
    conversation_id = str(request_metadata.get("agent_conversation_id") or f"system-event-{request_notification_id}")
    recent_diet_recommendations = get_recent_diet_recommendation_groups(session, patient_id=settings.patient_id)
    request = MultiturnChatRequest(
        patient_id=settings.patient_id,
        phr_patient_key=get_phr_patient_key(session),
        event_type=event_type,
        message=message,
        current_time=clock.current_time,
        context={
            "schedule_slots": schedule_slots,
            "resolved_policies": [
                resolve_policy_for_slot(session, settings.patient_id, slot_label, clock.current_time.date()).model_dump(mode="json")
                for slot_label in schedule_slots
            ],
            "policy_boundaries": [
                resolve_policy_boundary_for_slot(session, settings.patient_id, slot_label, clock.current_time.date()).model_dump(mode="json")
                for slot_label in schedule_slots
            ],
            "system_policies": [daily_pattern_conversation_time_view(session)],
            "recent_notifications": [row.body for row in get_notifications(session, clock.current_time)[:5]],
            "nutrition": build_nutrition_context(session, patient_id=settings.patient_id),
            "recent_diet_recommendations": recent_diet_recommendations,
            "recent_nutrition_alerts": [
                row.body
                for row in get_notifications(session, clock.current_time)
                if row.notification_type == "nutrition_alert" and row.patient_id == settings.patient_id
            ][:5],
            "request_metadata": request_metadata,
            MISSED_DOSE_REPLY_METADATA_KEY: request_metadata.get(MISSED_DOSE_REPLY_METADATA_KEY, {}),
            "today_dose_events": [
                {
                    "dose_event_id": row.id,
                    "medication_name": row.medication_name,
                    "slot_label": row.slot_label,
                    "scheduled_for": row.scheduled_for.isoformat(),
                    "status": row.status,
                }
                for row in get_today_dose_events(session, clock.current_time)
            ],
            "recent_chat": [turn.model_dump(mode="json") for turn in get_recent_chat_turns(session)],
        },
        callback_context=AgentCallbackContext(
            app_base_url=settings.system_base_url,
            notification_id=request_notification_id,
            conversation_id=conversation_id,
        ),
    )
    trace_logging.log_info(
        "system_event_request_payload_built",
        event_type=event_type,
        notification_id=request_notification_id,
        current_time=clock.current_time.isoformat(),
        phr_registered=bool(request.phr_patient_key),
        schedule_slots=schedule_slots,
        recent_chat_count=len(request.context.get("recent_chat", [])),
        recent_diet_recommendation_group_count=len(recent_diet_recommendations),
        today_dose_event_count=len(request.context.get("today_dose_events", [])),
    )
    return request


def apply_system_event_response(
    session: Session,
    event_type: str,
    message: str,
    request_notification_id: int,
    response: AgentResponse,
) -> None:
    trace_logging.log_info(
        "system_event_response_apply_started",
        notification_id=request_notification_id,
        trace_id=response.trace_id,
        agent=response.agent_name,
        decision=response.decision_type,
        summary=trace_logging.snippet(response.human_summary),
    )
    upsert_agent_run_trace(
        session,
        response,
        workflow_name=event_type,
        source_event_type="system_event_response",
        status="completed",
        request_id=str(request_notification_id),
        patient_id=settings.patient_id,
        request_message=message,
        notification_id=request_notification_id,
    )
    log_agent_tool_trace(request_notification_id, response)
    merge_missed_dose_reply_understanding_from_agent_response(session, request_notification_id, response)
    policy_tool_response = is_policy_tool_response(response)
    if not policy_tool_response:
        persist_agent_summary(session, response, category="multiturn_chat")
    dose_taken_result = maybe_apply_dose_taken_response(session, response, "multiturn_chat")
    if dose_taken_result is not None:
        applied, result_message = dose_taken_result
    else:
        applied, result_message = maybe_apply_policy_response(session, response, "multiturn_chat")
    policy_confirmation_requested = result_message in {"정책 변경 확인 알림을 보냈습니다.", "정책 변경 후보를 채팅에 표시했습니다."}
    if not applied and response.human_summary and not policy_tool_response:
        result_message = response.human_summary
    status = "applied" if applied else "answered"
    if policy_confirmation_requested:
        status = "needs_confirmation"
        result_message = "정책 변경 후보를 채팅에 표시했습니다."
    if response.decision_type == "system_policy_clarification":
        status = "needs_clarification"
    if policy_tool_response and not policy_confirmation_requested:
        add_chat_message(
            session,
            role="assistant",
            content=policy_tool_chat_message(status, result_message, response),
            sender_type="assistant",
            category="multiturn_chat",
        )
    update_system_event_request_notification(
        session,
        request_notification_id,
        status=status,
        request_message=message,
        result_message=result_message,
        response=response,
    )
    trace_logging.log_info(
        "system_event_response_apply_completed",
        notification_id=request_notification_id,
        trace_id=response.trace_id,
        status=status,
        applied=applied,
        result=trace_logging.snippet(result_message),
    )


def response_requires_async_continuation(response: AgentResponse) -> bool:
    return response.structured_payload.get("async_continuation_required") is True


def build_async_continuation_request(request: MultiturnChatRequest, response: AgentResponse) -> MultiturnChatRequest:
    continuation = request.model_copy(deep=True)
    context = dict(continuation.context or {})
    context["execute_async_continuation"] = True
    context["async_continuation_type"] = response.structured_payload.get("async_continuation_type", "")
    context["async_tool_calls"] = response.structured_payload.get("tool_calls", [])
    continuation.context = context
    return continuation


def apply_async_continuation_ack(
    session: Session,
    event_type: str,
    message: str,
    request_notification_id: int,
    response: AgentResponse,
) -> None:
    trace_logging.log_info(
        "system_event_async_continuation_ack",
        notification_id=request_notification_id,
        trace_id=response.trace_id,
        continuation_type=response.structured_payload.get("async_continuation_type"),
    )
    upsert_agent_run_trace(
        session,
        response,
        workflow_name=event_type,
        source_event_type="system_event_async_ack",
        status="awaiting_agent",
        request_id=str(request_notification_id),
        patient_id=settings.patient_id,
        request_message=message,
        notification_id=request_notification_id,
    )
    persist_agent_summary(session, response, category="multiturn_chat")
    update_system_event_request_notification(
        session,
        request_notification_id,
        status="answered",
        request_message=message,
        result_message=response.human_summary,
        response=response,
    )
    notification = session.get(Notification, request_notification_id)
    if notification is not None:
        metadata = parse_json_object(notification.metadata_json)
        metadata.update(
            {
                "async_continuation_status": "pending",
                "async_continuation_type": response.structured_payload.get("async_continuation_type", ""),
                "async_tool_count": len(response.structured_payload.get("tool_calls", []))
                if isinstance(response.structured_payload.get("tool_calls"), list)
                else 0,
            }
        )
        notification.metadata_json = dump_json(metadata)
        session.flush()


def mark_system_event_async_submitted(
    session: Session,
    event_type: str,
    message: str,
    request_notification_id: int,
    *,
    request_id: str = "",
    task_type: str = "chat_continuation",
) -> None:
    notification = session.get(Notification, request_notification_id)
    if notification is None:
        return
    metadata = parse_json_object(notification.metadata_json)
    metadata.update(
        {
            "status": "awaiting_agent",
            "request_message": message,
            "event_type": event_type,
            "async_continuation_status": "pending",
            "async_continuation_type": task_type,
            "agent_async_request_id": request_id,
        }
    )
    notification.title = "에이전트 답변 대기"
    notification.body = f"에이전트가 답변을 준비하고 있습니다: {message}"
    notification.metadata_json = dump_json(metadata)
    session.flush()


def log_agent_tool_trace(notification_id: int, response: AgentResponse) -> None:
    tool_calls = response.structured_payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        single_call = response.structured_payload.get("tool_call")
        tool_calls = [single_call] if isinstance(single_call, dict) else []
    tool_results = response.structured_payload.get("tool_results")
    if not isinstance(tool_results, list):
        tool_results = []
    if not tool_calls and not tool_results:
        return
    trace_logging.log_info(
        "system_event_agent_tool_trace",
        notification_id=notification_id,
        trace_id=response.trace_id,
        tool_count=len(tool_calls),
        tool_names=[str(call.get("name") or "") for call in tool_calls if isinstance(call, dict)],
        tool_calls=[_tool_call_log_payload(call) for call in tool_calls if isinstance(call, dict)],
        tool_results=[_tool_result_log_payload(result) for result in tool_results if isinstance(result, dict)],
    )


def _tool_call_log_payload(tool_call: dict) -> dict:
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    return {
        "tool": str(tool_call.get("name") or ""),
        "arguments": safe_log_arguments(arguments),
    }


def _tool_result_log_payload(result: dict) -> dict:
    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    tool_name = str(result.get("tool_name") or "")
    payload: dict = {
        "tool": tool_name,
        "status": result.get("status"),
        "error": trace_logging.snippet(result.get("error")),
        "idempotency_key_present": bool(result.get("idempotency_key")),
    }
    if tool_name == "lookup_side_effect_info":
        payload["response"] = {
            "suspected": response.get("suspected"),
            "matched_effects": response.get("matched_effects", []),
            "matched_items": response.get("matched_items", []),
            "severity": response.get("severity"),
            "evidence": trace_logging.snippet(response.get("evidence")),
        }
    elif tool_name == "AE_pro_ctcae":
        payload["response"] = {
            "input_symptom": trace_logging.snippet(response.get("input_symptom")),
            "matched": response.get("matched"),
            "match_type": response.get("match_type"),
            "matched_symptom_term": response.get("matched_symptom_term"),
            "matched_korean_symptom_name": response.get("matched_korean_symptom_name"),
            "similarity": response.get("similarity"),
            "question_count": len(response.get("questions", [])) if isinstance(response.get("questions"), list) else 0,
        }
    elif tool_name == "mark_dose_taken":
        payload["response"] = {
            "dose_event_id": response.get("dose_event_id"),
            "status": response.get("status"),
            "message": trace_logging.snippet(response.get("message")),
        }
    else:
        payload["response_keys"] = sorted(str(key) for key in response.keys())
    return payload


def mark_system_event_request_failed(
    session: Session,
    event_type: str,
    message: str,
    request_notification_id: int,
    error: Exception,
) -> None:
    error_type = getattr(error, "error_type", type(error).__name__)
    safe_error = safe_exception_summary(error)
    failure_copy = copy_for_agent_error(str(error_type), source_event_type=event_type)
    trace_logging.log_warning(
        "system_event_request_failed",
        event_type=event_type,
        notification_id=request_notification_id,
        error_type=type(error).__name__,
        error=safe_error,
    )
    update_system_event_request_notification(
        session,
        request_notification_id,
        status="failed",
        request_message=message,
        result_message=safe_error,
    )
    present_agent_error(
        session,
        "multiturn_chat",
        error,
        user_message=failure_copy.body,
        add_chat=True,
    )


def is_policy_tool_response(response: AgentResponse) -> bool:
    policy_tool_names = {"apply_notification_policy", "apply_system_policy"}
    tool_call = response.structured_payload.get("tool_call")
    if isinstance(tool_call, dict) and tool_call.get("name") in policy_tool_names:
        return True
    tool_calls = response.structured_payload.get("tool_calls")
    return isinstance(tool_calls, list) and any(isinstance(item, dict) and item.get("name") in policy_tool_names for item in tool_calls)


def policy_tool_chat_message(status: str, result_message: str, response: AgentResponse | None = None) -> str:
    if status == "needs_confirmation":
        return policy_confirmation_chat_message(response) or POLICY_CONFIRMATION_CHAT_MESSAGE
    if status == "applied":
        return result_message or "요청하신 알림 정책을 반영했습니다."
    return result_message or "정책 변경 후보를 처리하지 못했습니다. 바꾸고 싶은 알림 시간대, 횟수, 간격을 다시 알려주세요."


def policy_confirmation_chat_message(response: AgentResponse | None) -> str | None:
    if response is None:
        return None
    try:
        deltas = policy_deltas_from_tool_response(response)
    except (TypeError, ValueError):
        return None
    if not deltas:
        return None
    if len(deltas) == 1:
        delta = deltas[0]
        return f"{delta.slot_label} 알림을 {policy_delta_chat_details(delta)}으로 받는 후보를 만들었어요. {POLICY_CONFIRMATION_CHAT_SUFFIX}"
    lines = [f"- {delta.slot_label}: {policy_delta_chat_details(delta)}" for delta in deltas]
    return "정책 변경 후보를 만들었어요:\n" + "\n".join(lines) + f"\n{POLICY_CONFIRMATION_CHAT_SUFFIX}"


def policy_delta_chat_details(delta: NotificationPolicyDelta) -> str:
    parts: list[str] = []
    primary = primary_reminder_chat_label(delta.primary_reminder_timing, delta.primary_reminder_offset_minutes)
    if primary:
        parts.append(primary)
    parts.extend([f"추가 {delta.extra_reminders}회", f"{delta.interval_minutes}분 간격"])
    return ", ".join(parts)


def primary_reminder_chat_label(timing: str | None, offset_minutes: int | None) -> str | None:
    if timing == "before":
        return f"기본 알림 복약 {offset_minutes or 0}분 전"
    if timing == "after":
        return f"기본 알림 복약 {offset_minutes or 0}분 후"
    if timing == "at":
        return "기본 알림 정시"
    return None
