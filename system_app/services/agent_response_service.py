from __future__ import annotations

from datetime import date

from pydantic import ValidationError
from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.schemas import AgentResponse, NotificationPolicyDelta
from system_app.services.agent_client import AgentClient
from system_app.services.agent_error_service import present_agent_error
from system_app.services.agent_jobs import create_agent_job
from system_app.services.audit_service import record_agent_audit
from system_app.services.clock_service import ensure_clock, pause_simulation_clock_for_conversation
from system_app.services.dose_event_service import build_daily_pattern
from system_app.services.missed_dose_agent_response_service import missed_dose_adherence_pattern_message
from system_app.services.notification_service import acknowledge_duplicate_conversation_alerts, create_notification, get_unacknowledged_conversation_alert
from system_app.services.policy_confirmation import (
    create_policy_confirmation_alert,
    create_system_policy_confirmation_alert,
    has_notification_policy_tool_call,
    has_system_policy_tool_call,
    policy_deltas_from_tool_response,
    system_policy_deltas_from_tool_response,
)
from system_app.services.side_effect_reminder_safety import is_reminder_suppressed_after_side_effect
from system_app.services.timeline_service import add_chat_message, conversation_alert_chat_metadata, conversation_alert_visible_at, ensure_chat_message_for_conversation_alert

POLICY_DELTA_REQUIRED_FIELDS = {
    "slot_label",
    "extra_reminders",
    "interval_minutes",
    "effective_start_date",
    "effective_end_date",
    "reason",
    "source",
}


def ae_pro_ctcae_payload(response: AgentResponse) -> dict | None:
    direct = response.structured_payload.get("ae_pro_ctcae")
    if isinstance(direct, dict):
        return direct
    raw_results = response.structured_payload.get("tool_results")
    if not isinstance(raw_results, list):
        return None
    for result in raw_results:
        if not isinstance(result, dict):
            continue
        if result.get("tool_name") != "AE_pro_ctcae" or result.get("status") != "success":
            continue
        result_response = result.get("response")
        if isinstance(result_response, dict):
            return result_response
    return None


def ae_pro_ctcae_chat_metadata(response: AgentResponse) -> dict:
    payload = ae_pro_ctcae_payload(response)
    if payload is None:
        return {}
    return {
        "ae_pro_ctcae": {
            "input_symptom": payload.get("input_symptom", ""),
            "matched": bool(payload.get("matched")),
            "match_type": payload.get("match_type", ""),
            "matched_symptom_term": payload.get("matched_symptom_term", ""),
            "matched_korean_symptom_name": payload.get("matched_korean_symptom_name", ""),
            "similarity": payload.get("similarity", 0.0),
            "threshold": payload.get("threshold", 0.0),
            "sheet_name": payload.get("sheet_name", ""),
            "questions": payload.get("questions", []),
            "candidates": payload.get("candidates", []),
            "responses": [],
        }
    }


def food_selection_chat_metadata(response: AgentResponse) -> dict:
    food_searches = response.structured_payload.get("food_searches")
    # 단일 검색 결과도 처리 (food_searches 없을 때 폴백)
    if not isinstance(food_searches, list) or not food_searches:
        candidates = response.structured_payload.get("food_candidates")
        if not isinstance(candidates, list) or not candidates:
            return {}
        tool_call = response.structured_payload.get("tool_call")
        query = ""
        if isinstance(tool_call, dict) and tool_call.get("name") == "search_food_nutrition":
            query = str(tool_call.get("input", {}).get("query", "") or tool_call.get("arguments", {}).get("query", ""))
        food_searches = [{"query": query, "candidates": candidates}]

    first = food_searches[0]
    return {
        "food_selection": {
            "stage": "awaiting_food_choice",
            "query": first.get("query", ""),
            "candidates": first.get("candidates", []),
            "selected_food": None,
            "portion_g": None,
            "meal_type": None,
            "foods_queue": food_searches[1:],
            "confirmed_foods": [],
        }
    }


def policy_deltas_from_response_payload(response: AgentResponse) -> list[NotificationPolicyDelta]:
    raw_deltas = response.structured_payload.get("policy_deltas")
    if isinstance(raw_deltas, list):
        return [NotificationPolicyDelta.model_validate(item) for item in raw_deltas]
    if POLICY_DELTA_REQUIRED_FIELDS.intersection(response.structured_payload):
        return [NotificationPolicyDelta.model_validate(response.structured_payload)]
    return []


def persist_agent_summary(
    session: Session,
    response: AgentResponse,
    category: str,
    related_dose_event_id: int | None = None,
) -> None:
    existing = get_unacknowledged_conversation_alert(session, related_dose_event_id)
    conversation_notification = None
    message_metadata = ae_pro_ctcae_chat_metadata(response)
    food_metadata = food_selection_chat_metadata(response)
    if food_metadata:
        message_metadata.update(food_metadata)
    should_update_existing_missed_dose_alert = category == "missed_dose" and existing is not None
    if response.requires_conversation_alert or should_update_existing_missed_dose_alert:
        if existing is not None:
            clock = ensure_clock(session)
            existing_metadata = parse_json_object(existing.metadata_json)
            resume_state = existing_metadata.get("resume_clock", {})
        else:
            clock, resume_state = pause_simulation_clock_for_conversation(session)
            existing_metadata = {}
        visible_at = conversation_alert_visible_at(session, related_dose_event_id, clock.current_time)
        metadata = {**existing_metadata, "trace_id": response.trace_id, "category": category, "status": "agent_ready", "resume_clock": resume_state}
        fallback_body = "AI가 미복용 상황을 확인했습니다. 현재 상태와 복용하지 못한 이유를 알려주세요." if category == "missed_dose" else "AI가 복약 관련 대화를 요청하고 있습니다."
        pattern_message = ""
        if category == "missed_dose" and related_dose_event_id is not None:
            pattern_message = missed_dose_adherence_pattern_message(session, related_dose_event_id, message_metadata, response)
        body = pattern_message or response.human_summary or fallback_body
        if category == "missed_dose":
            metadata["agent_response_preview"] = body[:200]
        if existing is not None:
            existing.title = "AI가 대화를 요청합니다." if category == "missed_dose" else "AI 대화 알림"
            existing.body = body
            existing.visible_at = visible_at
            existing.metadata_json = dump_json(metadata)
            conversation_notification = existing
            session.flush()
        elif category != "missed_dose" or related_dose_event_id is None:
            conversation_notification = create_notification(
                session,
                notification_type="conversation_alert",
                title="AI가 대화를 요청합니다." if category == "missed_dose" else "AI 대화 알림",
                body=body,
                visible_at=visible_at,
                related_dose_event_id=related_dose_event_id,
                metadata=metadata,
            )
    if conversation_notification is not None and category == "missed_dose":
        acknowledge_duplicate_conversation_alerts(session, related_dose_event_id, conversation_notification.id)
        message_metadata = conversation_alert_chat_metadata(conversation_notification, message_metadata)
    message_content = response.human_summary
    if conversation_notification is not None and category == "missed_dose":
        message_content = message_content or conversation_notification.body
        if pattern_message and "ae_pro_ctcae" not in message_metadata:
            message_content = pattern_message
    if not message_content and "ae_pro_ctcae" in message_metadata:
        message_content = "PRO-CTCAE 자기보고 문항을 준비했어요. 아래 문항에 답해주세요."
    if not message_content and "food_selection" in message_metadata:
        fs_candidates = message_metadata["food_selection"].get("candidates", [])
        names = [str(c.get("food_name")) for c in fs_candidates[:3] if isinstance(c, dict) and c.get("food_name")]
        message_content = f"음식 후보를 찾았습니다. 아래에서 선택해주세요: {', '.join(names)}" if names else "음식 후보를 찾았습니다. 아래에서 선택해주세요."
    if conversation_notification is not None and category == "missed_dose":
        ensure_chat_message_for_conversation_alert(
            session,
            conversation_notification,
            content=message_content,
            metadata=message_metadata,
        )
    elif message_content:
        add_chat_message(
            session,
            role="assistant",
            content=message_content,
            sender_type="assistant",
            category=category,
            related_dose_event_id=related_dose_event_id,
            metadata=message_metadata,
        )


def maybe_apply_policy_response(session: Session, response: AgentResponse, source_event_type: str) -> tuple[bool, str]:
    tool_call = response.structured_payload.get("tool_call")
    if isinstance(tool_call, dict) and tool_call.get("name") not in {"apply_notification_policy", "apply_system_policy"}:
        record_agent_audit(session, response, source_event_type, applied=False, error_message="")
        return False, response.human_summary or "정책 변경 대상이 아닌 응답입니다."

    if response.decision_type == "tool_call":
        if response.structured_payload.get("tools_executed") is True and tool_results_have_error(response):
            return record_executed_tool_results(session, response, source_event_type)

        if has_notification_policy_tool_call(response):
            try:
                deltas = policy_deltas_from_tool_response(response, source_event_type)
            except (ValidationError, ValueError) as exc:
                record_agent_audit(session, response, source_event_type, applied=False, error_message=str(exc))
                raise
            return create_policy_confirmation_alert(session, response, source_event_type, deltas)

        if has_system_policy_tool_call(response):
            try:
                deltas = system_policy_deltas_from_tool_response(response, source_event_type)
            except (ValidationError, ValueError) as exc:
                record_agent_audit(session, response, source_event_type, applied=False, error_message=str(exc))
                raise
            return create_system_policy_confirmation_alert(session, response, source_event_type, deltas)

        if response.structured_payload.get("tools_executed") is True:
            return record_executed_tool_results(session, response, source_event_type)

        try:
            deltas = policy_deltas_from_tool_response(response)
        except (ValidationError, ValueError) as exc:
            record_agent_audit(session, response, source_event_type, applied=False, error_message=str(exc))
            raise

        return create_policy_confirmation_alert(session, response, source_event_type, deltas)

    if response.decision_type not in {
        "pattern_policy_recommendation",
        "patient_requested_policy_change",
    }:
        record_agent_audit(session, response, source_event_type, applied=False, error_message="")
        return False, response.human_summary or "정책 변경 대상이 아닌 응답입니다."

    try:
        deltas = policy_deltas_from_response_payload(response)
    except ValidationError as exc:
        record_agent_audit(session, response, source_event_type, applied=False, error_message=str(exc))
        raise
    if not deltas:
        record_agent_audit(session, response, source_event_type, applied=False, error_message="")
        return False, response.human_summary or "정책 변경 후보가 없습니다."

    return create_policy_confirmation_alert(session, response, source_event_type, deltas)


def tool_results_have_error(response: AgentResponse) -> bool:
    raw_results = response.structured_payload.get("tool_results")
    if not isinstance(raw_results, list):
        return False
    return any(isinstance(result, dict) and result.get("status") == "error" for result in raw_results)


def record_executed_tool_results(session: Session, response: AgentResponse, source_event_type: str) -> tuple[bool, str]:
    raw_results = response.structured_payload.get("tool_results")
    if not isinstance(raw_results, list):
        record_agent_audit(session, response, source_event_type, applied=False, error_message="실행된 tool result가 없습니다.")
        return False, "실행된 tool result가 없습니다."
    applied = all(isinstance(result, dict) and result.get("status") == "success" for result in raw_results)
    result_messages: list[str] = []
    for result in raw_results:
        if not isinstance(result, dict):
            continue
        response_payload = result.get("response") if isinstance(result.get("response"), dict) else {}
        items = response_payload.get("results") if isinstance(response_payload.get("results"), list) else []
        result_messages.extend(
            f"{item.get('slot_label') or item.get('policy_key')}: {item.get('message')}"
            for item in items
            if isinstance(item, dict) and (item.get("slot_label") or item.get("policy_key"))
        )
        if result.get("error"):
            result_messages.append(str(result["error"]))
    record_agent_audit(
        session,
        response,
        source_event_type,
        applied=applied,
        error_message="" if applied else " / ".join(result_messages),
    )
    return applied, " / ".join(result_messages) if result_messages else response.human_summary


def maybe_apply_dose_taken_response(session: Session, response: AgentResponse, source_event_type: str) -> tuple[bool, str] | None:
    tool_call = response.structured_payload.get("tool_call")
    if not isinstance(tool_call, dict) or tool_call.get("name") != "mark_dose_taken":
        return None

    if response.structured_payload.get("tools_executed") is True:
        raw_results = response.structured_payload.get("tool_results")
        if isinstance(raw_results, list):
            for result in raw_results:
                if not isinstance(result, dict) or result.get("tool_name") != "mark_dose_taken":
                    continue
                payload = result.get("response") if isinstance(result.get("response"), dict) else {}
                message = str(payload.get("message") or result.get("error") or response.human_summary or "")
                applied = result.get("status") == "success" and payload.get("status") == "taken"
                record_agent_audit(session, response, source_event_type, applied=applied, error_message="" if applied else message)
                return applied, message or response.human_summary
        record_agent_audit(session, response, source_event_type, applied=False, error_message="실행된 mark_dose_taken tool result가 없습니다.")
        return False, "실행된 복약 완료 tool result가 없습니다."

    record_agent_audit(session, response, source_event_type, applied=False, error_message="mark_dose_taken tool was not executed")
    return False, "실행된 복약 완료 tool result가 없습니다."

def build_manual_pattern_analysis_payload(session: Session, target_date: date | None = None):
    clock = ensure_clock(session)
    analysis_date = target_date or clock.current_time.date()
    return build_daily_pattern(session, analysis_date)


def persist_manual_pattern_analysis_response(session: Session, response: AgentResponse) -> None:
    persist_agent_summary(session, response, category="pattern_analysis")
    maybe_apply_policy_response(session, response, "manual_daily_pattern")
    session.commit()


def persist_manual_pattern_analysis_failure(session: Session, error: Exception) -> None:
    present_agent_error(
        session,
        "manual_daily_pattern",
        error,
        user_message="AI 일일 패턴 분석에 실패했습니다. 잠시 후 다시 시도해주세요.",
        add_chat=True,
    )
    session.commit()


async def run_manual_pattern_analysis(session: Session, agent_client: AgentClient, target_date: date | None = None) -> bool:
    if is_reminder_suppressed_after_side_effect(session):
        session.commit()
        return False
    pattern = build_manual_pattern_analysis_payload(session, target_date)
    if not pattern:
        session.commit()
        return False
    create_agent_job(session, "daily_pattern", pattern)
    session.commit()
    return True
