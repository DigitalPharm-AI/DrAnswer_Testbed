from __future__ import annotations

from typing import Any

from agent_app.tools.side_effects import side_effect_pro_ctcae_summary
from agent_app.tools.names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    POLICY_TOOLS,
    PROPOSE_NOTIFICATION_POLICY,
    PROPOSE_SYSTEM_POLICY,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from shared.redaction import redact_for_logging, redacted_clinical_text_label
from shared.schemas import ToolCallResult


def tool_calls_payload(tool_calls: list[dict[str, Any]], results: list[ToolCallResult]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_calls": tool_calls,
        "tool_results": [_safe_tool_result_payload(result) for result in results],
        "tools_executed": any(result.status != "skipped" for result in results),
    }
    if tool_calls:
        payload["tool_call"] = tool_calls[0]
    tool_argument_queues = _tool_argument_queues(tool_calls)
    for result in results:
        call_arguments = _next_tool_arguments(tool_argument_queues, result.tool_name)
        if result.tool_name == GET_PRO_CTCAE_QUESTIONNAIRE and result.status == "success":
            payload["ae_pro_ctcae"] = result.response
        elif result.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT:
            payload["side_effect_lookup"] = result.model_dump(mode="json")
            payload["side_effect_status"] = _side_effect_status(result)
        elif result.tool_name == GET_SIDE_EFFECT_HISTORY and result.status == "success":
            payload["side_effect_history"] = result.response.get("records", [])
            payload["side_effect_history_total"] = result.response.get("total", 0)
        elif result.tool_name == GET_MEDICATION_DOSE_STATUS and result.status == "success":
            payload["medication_dose_status"] = result.response
        elif result.tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS and result.status == "success":
            payload["dose_taken_result"] = result.response
        elif result.tool_name == CREATE_NUTRITION_MEAL_RECORD and result.status == "success":
            payload["nutrition_meal_result"] = result.response
        elif result.tool_name == UPDATE_NUTRITION_MEAL_RECORD and result.status == "success":
            payload["nutrition_meal_update_result"] = result.response
        elif result.tool_name == DELETE_NUTRITION_MEAL_RECORD and result.status == "success":
            payload["nutrition_meal_delete_result"] = result.response
        elif result.tool_name == UPDATE_NUTRITION_FOOD_RECORD and result.status == "success":
            payload["nutrition_food_update_result"] = result.response
        elif result.tool_name == DELETE_NUTRITION_FOOD_RECORD and result.status == "success":
            payload["nutrition_food_delete_result"] = result.response
        elif result.tool_name == GET_NUTRITION_DAILY_SUMMARY and result.status == "success":
            payload["nutrition_daily_summary"] = result.response.get("daily_summary")
        elif result.tool_name == GET_NUTRITION_MEAL_RECORD_LIST and result.status == "success":
            payload["nutrition_meals"] = result.response.get("meals", [])
        elif result.tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES and result.status == "success":
            meal_type = _valid_meal_type(result.response.get("meal_type") or call_arguments.get("meal_type"))
            search_entry = {
                "query": result.response.get("query", ""),
                "candidates": result.response.get("candidates", []),
                "meal_type": meal_type,
                "limit": result.response.get("limit") or call_arguments.get("limit") or 6,
            }
            if "food_searches" not in payload:
                payload["food_searches"] = []
            payload["food_searches"].append(search_entry)
            # 단일 검색 호환성 유지 (마지막 검색 결과)
            payload["food_candidates"] = result.response.get("candidates", [])
        elif result.tool_name == UPSERT_NUTRITION_PREFERENCE_FACT and result.status == "success":
            payload["nutrition_preference_result"] = result.response
        elif result.tool_name == GET_NUTRITION_PREFERENCE_SUMMARY and result.status == "success":
            payload["nutrition_preferences"] = result.response.get("preferences", {})
        elif result.tool_name == GET_NUTRITION_RECOMMENDATION_CANDIDATES and result.status == "success":
            payload["diet_recommendations"] = result.response.get("recommendations", [])
            payload["constraints_applied"] = result.response.get("constraints_applied", {})
            payload["blocked_count"] = result.response.get("blocked_count", 0)
            payload["total_candidates"] = result.response.get("total_candidates", 0)
            payload["meal_type_requested"] = result.response.get("meal_type_requested", "")
            payload["meal_candidate_count"] = result.response.get("meal_candidate_count", 0)
            payload["non_meal_candidate_count"] = result.response.get("non_meal_candidate_count", 0)
        elif result.tool_name == PROPOSE_NOTIFICATION_POLICY and result.status == "success":
            payload["policy_apply_result"] = result.response
        elif result.tool_name == PROPOSE_SYSTEM_POLICY and result.status == "success":
            payload["system_policy_apply_result"] = result.response
    return payload


def _tool_argument_queues(tool_calls: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    queues: dict[str, list[dict[str, Any]]] = {}
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or call.get("tool_name") or "")
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else call.get("args")
        if not name or not isinstance(arguments, dict):
            continue
        queues.setdefault(name, []).append(arguments)
    return queues


def _next_tool_arguments(queues: dict[str, list[dict[str, Any]]], tool_name: str) -> dict[str, Any]:
    queue = queues.get(tool_name)
    if not queue:
        return {}
    return queue.pop(0)


def _valid_meal_type(value: Any) -> str:
    text = str(value or "")
    return text if text in {"breakfast", "lunch", "dinner", "snack"} else ""


def tool_result_summary(results: list[ToolCallResult], fallback: str) -> str:
    if not results:
        return fallback
    last = results[-1]
    if last.tool_name == GET_PRO_CTCAE_QUESTIONNAIRE:
        if last.status == "success" and last.response.get("matched"):
            side_effect_summary = side_effect_pro_ctcae_summary(results)
            if side_effect_summary:
                return side_effect_summary
            return "증상에 맞는 PRO-CTCAE 자기보고 문항을 준비했습니다."
        if last.status == "success":
            return "정확히 맞는 PRO-CTCAE 증상명은 없어서 그 외 증상 문항으로 확인하겠습니다."
        return "PRO-CTCAE 문항을 불러오지 못했습니다. 증상이 심하거나 지속되면 의료진 또는 약사에게 확인하세요."
    if last.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT:
        if last.status == "success" and last.response.get("suspected"):
            return "PHR 주의사항 조회 결과 복용 중인 품목과 관련 가능성이 확인되었습니다."
        if last.status == "success":
            return "현재 PHR 기준으로 직접 일치하는 대표 부작용은 확인되지 않았습니다."
        return "부작용 정보를 조회하지 못했습니다. 증상이 심하거나 지속되면 의료진 또는 약사에게 확인하세요."
    if last.tool_name == GET_SIDE_EFFECT_HISTORY:
        if last.status != "success":
            return _safe_result_error(last.error) or "부작용 이력을 조회하지 못했습니다."
        records = last.response.get("records") if isinstance(last.response.get("records"), list) else []
        suspected_count = sum(1 for item in records if isinstance(item, dict) and item.get("suspected") is True)
        return f"부작용 이력 {len(records)}건을 확인했습니다. 의심 기록은 {suspected_count}건입니다."
    if last.tool_name == GET_MEDICATION_DOSE_STATUS:
        if last.status != "success":
            return _safe_result_error(last.error) or "복약 상태를 조회하지 못했습니다."
        events = last.response.get("dose_events") if isinstance(last.response.get("dose_events"), list) else []
        counts = {"scheduled": 0, "taken": 0, "missed": 0}
        for item in events:
            if isinstance(item, dict):
                status = str(item.get("status") or "")
                counts[status] = counts.get(status, 0) + 1
        date_label = _date_range_label(last.response)
        return f"{date_label} 복약 일정은 총 {len(events)}건이고, 완료 {counts.get('taken', 0)}건, 미복용 {counts.get('missed', 0)}건, 예정 {counts.get('scheduled', 0)}건입니다."
    if last.tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        return str(last.response.get("message") or _safe_result_error(last.error) or fallback)
    if last.tool_name == CREATE_NUTRITION_MEAL_RECORD:
        if last.status != "success":
            return _safe_result_error(last.error) or "식사 기록을 처리하지 못했습니다."
        meal = last.response.get("meal") if isinstance(last.response.get("meal"), dict) else {}
        summary = last.response.get("daily_summary") if isinstance(last.response.get("daily_summary"), dict) else {}
        exceeded = summary.get("summary", {}).get("exceeded_nutrients") if isinstance(summary.get("summary"), dict) else []
        meal_label = meal.get("meal_label") or meal.get("meal_type") or "식사"
        if exceeded:
            return f"{meal_label} 식사를 기록했습니다. 오늘 기준 초과 항목은 {', '.join(exceeded)}입니다."
        return f"{meal_label} 식사를 기록했습니다. 현재까지 초과 항목은 없습니다."
    if last.tool_name == GET_NUTRITION_DAILY_SUMMARY:
        if last.status != "success":
            return _safe_result_error(last.error) or "오늘 영양 요약을 조회하지 못했습니다."
        summary = last.response.get("daily_summary") if isinstance(last.response.get("daily_summary"), dict) else {}
        total_meals = summary.get("total_meals", 0)
        exceeded = summary.get("summary", {}).get("exceeded_nutrients") if isinstance(summary.get("summary"), dict) else []
        if exceeded:
            return f"오늘 식사 {total_meals}건 기준으로 {', '.join(exceeded)} 섭취량이 기준을 초과했습니다."
        return f"오늘 식사 {total_meals}건 기준으로 현재 초과 항목은 없습니다."
    if last.tool_name == GET_NUTRITION_MEAL_RECORD_LIST:
        if last.status != "success":
            return _safe_result_error(last.error) or "식사 목록을 조회하지 못했습니다."
        total = last.response.get("total", 0)
        return f"해당 날짜에 기록된 식사는 {total}건입니다."
    if last.tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES:
        if last.status != "success":
            return _safe_result_error(last.error) or "음식 후보를 찾지 못했습니다."
        candidates = last.response.get("candidates") if isinstance(last.response.get("candidates"), list) else []
        if not candidates:
            return "일치하는 음식 후보를 찾지 못했습니다. 음식명을 조금 더 구체적으로 알려주세요."
        names = [str(item.get("food_name")) for item in candidates[:3] if isinstance(item, dict) and item.get("food_name")]
        return "음식 후보를 찾았습니다: " + ", ".join(names)
    if last.tool_name == UPSERT_NUTRITION_PREFERENCE_FACT:
        if last.status != "success":
            return _safe_result_error(last.error) or "영양 선호도를 저장하지 못했습니다."
        fact = last.response.get("fact") if isinstance(last.response.get("fact"), dict) else {}
        label = fact.get("object_label") or "해당 항목"
        predicate = fact.get("predicate_label") or fact.get("predicate") or "선호도"
        return f"{label}에 대한 {predicate} 정보를 저장했습니다."
    if last.tool_name == GET_NUTRITION_RECOMMENDATION_CANDIDATES:
        if last.status != "success":
            return _safe_result_error(last.error) or "식단 추천을 처리하지 못했습니다."
        recommendations = last.response.get("recommendations") if isinstance(last.response.get("recommendations"), list) else []
        if not recommendations:
            return "현재 조건에 맞는 음식을 찾지 못했습니다. 제약 조건을 조정하거나 다른 음식을 검색해보세요."
        names = [str(r.get("food_name", "")) for r in recommendations[:3] if isinstance(r, dict) and r.get("food_name")]
        blocked = last.response.get("blocked_count", 0)
        text = f"조건에 맞는 음식 {len(recommendations)}가지를 찾았습니다: {', '.join(names)}"
        if blocked:
            text += f" (선호도 제한으로 {blocked}가지 제외됨)"
        return text
    if last.tool_name == GET_NUTRITION_PREFERENCE_SUMMARY:
        if last.status != "success":
            return _safe_result_error(last.error) or "영양 선호도를 조회하지 못했습니다."
        preferences = last.response.get("preferences") if isinstance(last.response.get("preferences"), dict) else {}
        counts = preferences.get("counts") if isinstance(preferences.get("counts"), dict) else {}
        return f"저장된 영양 선호도는 제한 {counts.get('hard', 0)}건, 선호/비선호 {counts.get('soft', 0)}건입니다."
    if last.tool_name in POLICY_TOOLS:
        if last.status == "skipped" and last.response.get("reason") == "policy_confirmation_required":
            if last.tool_name == PROPOSE_SYSTEM_POLICY:
                return "시스템 정책 변경 후보를 만들었습니다. 확인 후 반영됩니다."
            return "알림 정책 변경 후보를 만들었습니다. 확인 후 반영됩니다."
        messages = _result_messages(last.response)
        return " / ".join(messages) if messages else _safe_result_error(last.error) or fallback
    return _safe_result_error(last.error) or fallback


def _side_effect_status(result: ToolCallResult) -> str:
    if result.status != "success":
        return "verification_unavailable"
    suspected = result.response.get("suspected")
    if isinstance(suspected, bool):
        return "suspected" if suspected else "not_suspected"
    return "verification_unavailable"


def _result_messages(response: dict[str, Any]) -> list[str]:
    items = response.get("results") if isinstance(response.get("results"), list) else []
    messages = []
    for item in items:
        if isinstance(item, dict) and item.get("message"):
            messages.append(str(item["message"]))
    return messages


def _safe_result_error(error: str) -> str:
    text = str(error or "").strip()
    if not text:
        return ""
    if len(text) <= 80 and all(char.isascii() and (char.isalnum() or char in "_:-.") for char in text):
        return text
    return redacted_clinical_text_label(text, key="error")


def _date_range_label(response: dict[str, Any]) -> str:
    if response.get("target_date"):
        return str(response["target_date"])
    start_date = str(response.get("start_date") or "")
    end_date = str(response.get("end_date") or "")
    if start_date and end_date:
        return start_date if start_date == end_date else f"{start_date}~{end_date}"
    return "해당 기간"


def _safe_tool_result_payload(result: ToolCallResult) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    if result.status == "error":
        payload["response"] = redact_for_logging(result.response)
        payload["error"] = _safe_result_error(result.error)
    return payload
