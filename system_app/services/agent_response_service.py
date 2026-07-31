from __future__ import annotations

from sqlalchemy.orm import Session

from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    POLICY_TOOLS,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
)
from shared.json_utils import dump_json, parse_json_object
from shared.schemas import AgentResponse, NotificationPolicyDelta
from system_app.models import ChatMessage
from system_app.services.clock_service import ensure_clock, pause_simulation_clock_for_conversation
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
NUTRITION_WRITE_TOOL_NAMES = {
    CREATE_NUTRITION_MEAL_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    UPDATE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
}
FOOD_SELECTION_CANDIDATE_LIMIT = 6
SUPPORTED_MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack"}


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
        if result.get("tool_name") != GET_PRO_CTCAE_QUESTIONNAIRE or result.get("status") != "success":
            continue
        result_response = result.get("response")
        if isinstance(result_response, dict):
            return result_response
    return None


def side_effect_record_draft_payload(response: AgentResponse) -> dict | None:
    direct = response.structured_payload.get("side_effect_record_draft")
    if isinstance(direct, dict):
        return direct
    raw_results = response.structured_payload.get("tool_results")
    if not isinstance(raw_results, list):
        return None
    for result in raw_results:
        if not isinstance(result, dict):
            continue
        if result.get("tool_name") != GET_MEDICATION_SIDE_EFFECT_ASSESSMENT or result.get("status") != "success":
            continue
        result_response = result.get("response")
        if not isinstance(result_response, dict):
            continue
        draft = result_response.get("side_effect_record_draft")
        if isinstance(draft, dict):
            return draft
    return None


def ae_pro_ctcae_chat_metadata(response: AgentResponse) -> dict:
    metadata: dict = {}
    payload = ae_pro_ctcae_payload(response)
    if payload is not None:
        metadata["ae_pro_ctcae"] = {
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
    draft = side_effect_record_draft_payload(response)
    if draft is not None:
        metadata["side_effect_record_draft"] = draft
    return metadata


def food_selection_chat_metadata(response: AgentResponse) -> dict:
    if has_successful_nutrition_write(response):
        return {}
    food_searches = response.structured_payload.get("food_searches")
    # 단일 검색 결과도 처리 (food_searches 없을 때 폴백)
    if not isinstance(food_searches, list) or not food_searches:
        candidates = response.structured_payload.get("food_candidates")
        if not isinstance(candidates, list) or not candidates:
            return {}
        tool_call = response.structured_payload.get("tool_call")
        query = ""
        meal_type = ""
        if isinstance(tool_call, dict) and tool_call.get("name") == SEARCH_NUTRITION_FOOD_CANDIDATES:
            arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else tool_call.get("input", {})
            query = str(arguments.get("query", "")) if isinstance(arguments, dict) else ""
            meal_type = _valid_meal_type(arguments.get("meal_type")) if isinstance(arguments, dict) else ""
        food_searches = [{"query": query, "candidates": candidates, "meal_type": meal_type}]
    food_searches = [_food_search_entry(row) for row in food_searches if isinstance(row, dict)]
    if not food_searches:
        return {}

    first = food_searches[0]
    return {
        "food_selection": {
            "stage": "awaiting_food_choice",
            "query": first.get("query", ""),
            "candidates": first.get("candidates", []),
            "selected_food": None,
            "portion_g": None,
            "meal_type": None,
            "default_meal_type": first.get("meal_type", ""),
            "foods_queue": food_searches[1:],
            "confirmed_foods": [],
        }
    }


def _food_search_entry(row: dict) -> dict:
    candidates = row.get("candidates") if isinstance(row.get("candidates"), list) else []
    return {
        "query": row.get("query", ""),
        "candidates": candidates[:FOOD_SELECTION_CANDIDATE_LIMIT],
        "meal_type": _valid_meal_type(row.get("meal_type")),
    }


def _valid_meal_type(value) -> str:
    text = str(value or "")
    return text if text in SUPPORTED_MEAL_TYPES else ""


def has_successful_nutrition_write(response: AgentResponse) -> bool:
    raw_results = response.structured_payload.get("tool_results")
    if isinstance(raw_results, list):
        for result in raw_results:
            if not isinstance(result, dict):
                continue
            if result.get("tool_name") in NUTRITION_WRITE_TOOL_NAMES and result.get("status") == "success":
                return True
    for key in (
        "nutrition_meal_result",
        "nutrition_meal_update_result",
        "nutrition_meal_delete_result",
        "nutrition_food_update_result",
        "nutrition_food_delete_result",
    ):
        payload = response.structured_payload.get(key)
        if isinstance(payload, dict) and payload.get("success") is not False:
            return True
    return False


def diet_recommendation_chat_metadata(response: AgentResponse) -> dict:
    recommendations = response.structured_payload.get("diet_recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        return {}
    return {
        "diet_recommendations": {
            "recommendations": recommendations,
            "constraints_applied": response.structured_payload.get("constraints_applied", {}),
            "blocked_count": response.structured_payload.get("blocked_count", 0),
            "total_candidates": response.structured_payload.get("total_candidates", 0),
            "meal_type_requested": response.structured_payload.get("meal_type_requested", ""),
            "meal_candidate_count": response.structured_payload.get("meal_candidate_count", 0),
            "non_meal_candidate_count": response.structured_payload.get("non_meal_candidate_count", 0),
        }
    }


def policy_deltas_from_response_payload(response: AgentResponse) -> list[NotificationPolicyDelta]:
    raw_deltas = response.structured_payload.get("policy_deltas")
    if isinstance(raw_deltas, list):
        return [NotificationPolicyDelta.model_validate(item) for item in raw_deltas]
    if POLICY_DELTA_REQUIRED_FIELDS.intersection(response.structured_payload):
        return [NotificationPolicyDelta.model_validate(response.structured_payload)]
    return []


def mutation_confirmation_chat_metadata(response: AgentResponse) -> dict:
    if response.structured_payload.get("mutation_confirmation_required") is not True:
        return {}
    proposal = response.structured_payload.get("mutation_confirmation")
    if not isinstance(proposal, dict):
        return {}
    display = (
        proposal.get("display")
        if isinstance(proposal.get("display"), dict)
        else {}
    )
    return {
        "record_approval_card": {
            "status": proposal.get("status", "pending"),
            "action_type": proposal.get("action_type", "agent_tool"),
            "action_name": proposal.get("action_name", ""),
            "display": display,
        }
    }


def persist_agent_summary(
    session: Session,
    response: AgentResponse,
    category: str,
    related_dose_event_id: int | None = None,
) -> ChatMessage | None:
    human_summary = str(response.human_summary or "").strip()
    if not human_summary:
        raise ValueError("agent_human_summary_missing")
    if category == "missed_dose" and related_dose_event_id is None:
        raise ValueError("missed_dose_event_context_missing")

    existing = get_unacknowledged_conversation_alert(session, related_dose_event_id)
    conversation_notification = None
    message_metadata = ae_pro_ctcae_chat_metadata(response)
    food_metadata = food_selection_chat_metadata(response)
    if food_metadata:
        message_metadata.update(food_metadata)
    diet_metadata = diet_recommendation_chat_metadata(response)
    if diet_metadata:
        message_metadata.update(diet_metadata)
    confirmation_metadata = mutation_confirmation_chat_metadata(response)
    if confirmation_metadata:
        message_metadata.update(confirmation_metadata)
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
        existing_metadata.pop("trace_id", None)
        metadata = {
            **existing_metadata,
            "category": category,
            "status": "agent_ready",
            "resume_clock": resume_state,
        }
        pattern_message = ""
        if category == "missed_dose" and related_dose_event_id is not None:
            pattern_message = missed_dose_adherence_pattern_message(
                session,
                related_dose_event_id,
                message_metadata,
                response,
            )
        body = pattern_message or human_summary
        if category == "missed_dose":
            metadata["agent_response_preview"] = human_summary[:200]
            metadata["feedback_message_source"] = "llm_generated"
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
    message_content = (
        conversation_notification.body
        if conversation_notification is not None
        and category == "missed_dose"
        else human_summary
    )
    persisted_message = None
    if conversation_notification is not None and category == "missed_dose":
        persisted_message = ensure_chat_message_for_conversation_alert(
            session,
            conversation_notification,
            content=message_content,
            metadata=message_metadata,
        )
        if persisted_message is not None:
            notification_metadata = parse_json_object(
                conversation_notification.metadata_json,
            )
            notification_metadata["chat_message_id"] = (
                persisted_message.public_id
            )
            conversation_notification.metadata_json = dump_json(
                notification_metadata,
            )
            session.flush()
    elif message_content:
        persisted_message = add_chat_message(
            session,
            role="assistant",
            content=message_content,
            sender_type="assistant",
            category=category,
            related_dose_event_id=related_dose_event_id,
            metadata=message_metadata,
        )
    return persisted_message


def maybe_apply_policy_response(session: Session, response: AgentResponse, source_event_type: str) -> tuple[bool, str]:
    tool_call = response.structured_payload.get("tool_call")
    if isinstance(tool_call, dict) and tool_call.get("name") not in POLICY_TOOLS:
        return False, response.human_summary or "정책 변경 대상이 아닌 응답입니다."

    if response.decision_type == "tool_call":
        if response.structured_payload.get("tools_executed") is True and tool_results_have_error(response):
            return record_executed_tool_results(session, response, source_event_type)

        if has_notification_policy_tool_call(response):
            deltas = policy_deltas_from_tool_response(response, source_event_type)
            return create_policy_confirmation_alert(session, response, source_event_type, deltas)

        if has_system_policy_tool_call(response):
            deltas = system_policy_deltas_from_tool_response(response, source_event_type)
            return create_system_policy_confirmation_alert(session, response, source_event_type, deltas)

        if response.structured_payload.get("tools_executed") is True:
            return record_executed_tool_results(session, response, source_event_type)

        deltas = policy_deltas_from_tool_response(response)
        return create_policy_confirmation_alert(session, response, source_event_type, deltas)

    if response.decision_type not in {
        "pattern_policy_recommendation",
        "patient_requested_policy_change",
    }:
        return False, response.human_summary or "정책 변경 대상이 아닌 응답입니다."

    deltas = policy_deltas_from_response_payload(response)
    if not deltas:
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
    return applied, " / ".join(result_messages) if result_messages else response.human_summary


def maybe_apply_dose_taken_response(session: Session, response: AgentResponse, source_event_type: str) -> tuple[bool, str] | None:
    tool_call = response.structured_payload.get("tool_call")
    if not isinstance(tool_call, dict) or tool_call.get("name") != UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        return None

    if response.structured_payload.get("tools_executed") is True:
        raw_results = response.structured_payload.get("tool_results")
        if isinstance(raw_results, list):
            for result in raw_results:
                if not isinstance(result, dict) or result.get("tool_name") != UPDATE_MEDICATION_DOSE_EVENT_STATUS:
                    continue
                payload = result.get("response") if isinstance(result.get("response"), dict) else {}
                message = str(payload.get("message") or result.get("error") or response.human_summary or "")
                applied = result.get("status") == "success" and payload.get("status") == "taken"
                return applied, message or response.human_summary
        return False, "실행된 복약 완료 tool result가 없습니다."

    return False, "실행된 복약 완료 tool result가 없습니다."
