from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_app.tools.side_effects import side_effect_pro_ctcae_summary
from shared.nutrition_domain import normalize_meal_type
from shared.redaction import redact_for_logging, redacted_clinical_text_label
from shared.schemas import ToolCallResult
from shared.tool_names import (
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


def public_tool_calls(
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove one-time capabilities from response and trace projections."""

    public_calls: list[dict[str, Any]] = []
    for call in tool_calls:
        public_call = dict(call)
        arguments = call.get("arguments")
        if isinstance(arguments, dict) and "approval_key" in arguments:
            public_call["arguments"] = {
                key: value
                for key, value in arguments.items()
                if key != "approval_key"
            }
            public_call["arguments"]["approval_key_present"] = True
        public_calls.append(public_call)
    return public_calls


def tool_calls_payload(
    tool_calls: list[dict[str, Any]],
    results: list[ToolCallResult],
) -> dict[str, Any]:
    public_calls = public_tool_calls(tool_calls)
    payload: dict[str, Any] = {
        "tool_calls": public_calls,
        "tool_results": [_safe_tool_result_payload(result) for result in results],
        "tools_executed": any(result.status != "skipped" for result in results),
    }
    if public_calls:
        payload["tool_call"] = public_calls[0]
    tool_argument_queues = _tool_argument_queues(tool_calls)
    for result in results:
        call_arguments = _next_tool_arguments(tool_argument_queues, result.tool_name)
        _project_tool_result(payload, result, call_arguments)
    return payload


_FULL_RESPONSE_PROJECTIONS = {
    GET_MEDICATION_DOSE_STATUS: "medication_dose_status",
    UPDATE_MEDICATION_DOSE_EVENT_STATUS: "dose_taken_result",
    CREATE_NUTRITION_MEAL_RECORD: "nutrition_meal_result",
    UPDATE_NUTRITION_MEAL_RECORD: "nutrition_meal_update_result",
    DELETE_NUTRITION_MEAL_RECORD: "nutrition_meal_delete_result",
    UPDATE_NUTRITION_FOOD_RECORD: "nutrition_food_update_result",
    DELETE_NUTRITION_FOOD_RECORD: "nutrition_food_delete_result",
    UPSERT_NUTRITION_PREFERENCE_FACT: "nutrition_preference_result",
    PROPOSE_NOTIFICATION_POLICY: "policy_apply_result",
    PROPOSE_SYSTEM_POLICY: "system_policy_apply_result",
}

ToolResultProjector = Callable[
    [dict[str, Any], ToolCallResult, dict[str, Any]],
    None,
]


def _project_tool_result(
    payload: dict[str, Any],
    result: ToolCallResult,
    call_arguments: dict[str, Any],
) -> None:
    if result.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT:
        _project_side_effect_lookup(payload, result, call_arguments)
        return
    if result.status != "success":
        return
    if result.tool_name == GET_PRO_CTCAE_QUESTIONNAIRE:
        questionnaires = payload.setdefault("ae_pro_ctcae_items", [])
        if isinstance(questionnaires, list):
            questionnaires.append(result.response)
        payload.setdefault("ae_pro_ctcae", result.response)
        return
    response_key = _FULL_RESPONSE_PROJECTIONS.get(result.tool_name)
    if response_key:
        payload[response_key] = result.response
        return
    projector = _SUCCESS_RESULT_PROJECTORS.get(result.tool_name)
    if projector:
        projector(payload, result, call_arguments)


def _project_side_effect_lookup(
    payload: dict[str, Any],
    result: ToolCallResult,
    _call_arguments: dict[str, Any],
) -> None:
    payload["side_effect_lookup"] = result.model_dump(mode="json")
    payload["side_effect_status"] = _side_effect_status(result)


def _project_side_effect_history(
    payload: dict[str, Any],
    result: ToolCallResult,
    _call_arguments: dict[str, Any],
) -> None:
    payload["side_effect_history"] = result.response.get("records", [])
    payload["side_effect_history_total"] = result.response.get("total", 0)


def _project_nutrition_daily_summary(
    payload: dict[str, Any],
    result: ToolCallResult,
    _call_arguments: dict[str, Any],
) -> None:
    payload["nutrition_daily_summary"] = result.response.get("daily_summary")


def _project_nutrition_meal_list(
    payload: dict[str, Any],
    result: ToolCallResult,
    _call_arguments: dict[str, Any],
) -> None:
    payload["nutrition_meals"] = result.response.get("meals", [])


def _project_nutrition_preferences(
    payload: dict[str, Any],
    result: ToolCallResult,
    _call_arguments: dict[str, Any],
) -> None:
    payload["nutrition_preferences"] = result.response.get("preferences", {})


def _project_diet_recommendations(
    payload: dict[str, Any],
    result: ToolCallResult,
    _call_arguments: dict[str, Any],
) -> None:
    response = result.response
    payload.update(
        {
            "diet_recommendations": response.get("recommendations", []),
            "constraints_applied": response.get("constraints_applied", {}),
            "blocked_count": response.get("blocked_count", 0),
            "total_candidates": response.get("total_candidates", 0),
            "meal_type_requested": response.get("meal_type_requested", ""),
            "meal_candidate_count": response.get("meal_candidate_count", 0),
            "non_meal_candidate_count": response.get(
                "non_meal_candidate_count",
                0,
            ),
        }
    )


def _project_food_searches(
    payload: dict[str, Any],
    result: ToolCallResult,
    call_arguments: dict[str, Any],
) -> None:
    meal_type = normalize_meal_type(
        result.response.get("meal_type") or call_arguments.get("meal_type")
    )
    raw_groups = result.response.get("search_groups")
    if not isinstance(raw_groups, list):
        raw_groups = [
            {
                "query": result.response.get("query", ""),
                "candidates": result.response.get("candidates", []),
            }
        ]
    limit = (
        result.response.get("limit_per_query")
        or call_arguments.get("limit_per_query")
        or 6
    )
    new_search_entries = [
        {
            "query": group.get("query", ""),
            "candidates": group["candidates"],
            "meal_type": meal_type,
            "limit": limit,
        }
        for group in raw_groups
        if isinstance(group, dict) and isinstance(group.get("candidates"), list)
    ]
    payload.setdefault("food_searches", []).extend(new_search_entries)
    if not _can_start_food_selection(payload, new_search_entries):
        return
    first_search = new_search_entries[0]
    # The external contract renders one selection_box at a time. Keep the
    # first group as the current card; the Agent DB owns the remaining order.
    payload["food_candidates"] = first_search["candidates"]
    payload["food_selection_progress"] = {
        "current_group": 1,
        "total_groups": len(new_search_entries),
        "query": first_search["query"],
    }


def _can_start_food_selection(
    payload: dict[str, Any],
    search_entries: list[dict[str, Any]],
) -> bool:
    return (
        bool(search_entries)
        and all(entry["candidates"] for entry in search_entries)
        and "food_candidates" not in payload
    )


_SUCCESS_RESULT_PROJECTORS: dict[str, ToolResultProjector] = {
    GET_SIDE_EFFECT_HISTORY: _project_side_effect_history,
    GET_NUTRITION_DAILY_SUMMARY: _project_nutrition_daily_summary,
    GET_NUTRITION_MEAL_RECORD_LIST: _project_nutrition_meal_list,
    SEARCH_NUTRITION_FOOD_CANDIDATES: _project_food_searches,
    GET_NUTRITION_PREFERENCE_SUMMARY: _project_nutrition_preferences,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES: (
        _project_diet_recommendations
    ),
}


def _tool_argument_queues(
    tool_calls: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    queues: dict[str, list[dict[str, Any]]] = {}
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or call.get("tool_name") or "")
        arguments = (
            call.get("arguments")
            if isinstance(call.get("arguments"), dict)
            else call.get("args")
        )
        if not name or not isinstance(arguments, dict):
            continue
        queues.setdefault(name, []).append(arguments)
    return queues


def _next_tool_arguments(
    queues: dict[str, list[dict[str, Any]]],
    tool_name: str,
) -> dict[str, Any]:
    queue = queues.get(tool_name)
    if not queue:
        return {}
    return queue.pop(0)


def tool_result_summary(results: list[ToolCallResult], fallback: str) -> str:
    if not results:
        return fallback
    last = results[-1]
    summarizer = _TOOL_RESULT_SUMMARIZERS.get(last.tool_name)
    if summarizer:
        return summarizer(results, last, fallback)
    return _safe_result_error(last.error) or fallback


ToolResultSummarizer = Callable[
    [list[ToolCallResult], ToolCallResult, str],
    str,
]


def _summarize_pro_ctcae(
    results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status == "success" and last.response.get("matched"):
        summary = side_effect_pro_ctcae_summary(results)
        return summary or (
            "증상에 맞는 PRO-CTCAE 자기보고 문항을 준비했습니다."
        )
    if last.status == "success":
        return (
            "정확히 맞는 PRO-CTCAE 증상명은 없어서 "
            "그 외 증상 문항으로 확인하겠습니다."
        )
    return (
        "PRO-CTCAE 문항을 불러오지 못했습니다. 증상이 심하거나 "
        "지속되면 의료진 또는 약사에게 확인하세요."
    )


def _summarize_side_effect_lookup(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status == "success" and last.response.get("suspected"):
        return (
            "현재 복약정보와 의약품 부작용 기준정보에서 "
            "관련 가능성이 확인되었습니다."
        )
    if last.status == "success":
        return (
            "현재 복약정보와 기준정보에서 직접 일치하는 "
            "대표 부작용은 확인되지 않았습니다."
        )
    return (
        "부작용 정보를 조회하지 못했습니다. 증상이 심하거나 "
        "지속되면 의료진 또는 약사에게 확인하세요."
    )


def _summarize_side_effect_history(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "부작용 이력을 조회하지 못했습니다."
        )
    records = _response_list(last, "records")
    suspected_count = sum(
        1
        for item in records
        if isinstance(item, dict) and item.get("suspected") is True
    )
    return (
        f"부작용 이력 {len(records)}건을 확인했습니다. "
        f"의심 기록은 {suspected_count}건입니다."
    )


def _summarize_medication_dose_status(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "복약 상태를 조회하지 못했습니다."
        )
    events = _response_list(last, "dose_events")
    counts = {"scheduled": 0, "taken": 0, "missed": 0}
    for item in events:
        if isinstance(item, dict):
            status = str(item.get("status") or "")
            counts[status] = counts.get(status, 0) + 1
    date_label = _date_range_label(last.response)
    return (
        f"{date_label} 복약 일정은 총 {len(events)}건이고, "
        f"완료 {counts.get('taken', 0)}건, "
        f"미복용 {counts.get('missed', 0)}건, "
        f"예정 {counts.get('scheduled', 0)}건입니다."
    )


def _summarize_medication_write(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    fallback: str,
) -> str:
    return str(
        last.response.get("message")
        or _safe_result_error(last.error)
        or fallback
    )


def _summarize_meal_write(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "식사 기록을 처리하지 못했습니다."
        )
    meal = _response_dict(last, "meal")
    summary = _response_dict(last, "daily_summary")
    summary_detail = summary.get("summary")
    exceeded = (
        summary_detail.get("exceeded_nutrients")
        if isinstance(summary_detail, dict)
        else []
    )
    meal_label = meal.get("meal_label") or meal.get("meal_type") or "식사"
    if exceeded:
        return (
            f"{meal_label} 식사를 기록했습니다. 오늘 기준 초과 항목은 "
            f"{', '.join(exceeded)}입니다."
        )
    return (
        f"{meal_label} 식사를 기록했습니다. "
        "현재까지 초과 항목은 없습니다."
    )


def _summarize_daily_nutrition(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "오늘 영양 요약을 조회하지 못했습니다."
        )
    summary = _response_dict(last, "daily_summary")
    summary_detail = summary.get("summary")
    exceeded = (
        summary_detail.get("exceeded_nutrients")
        if isinstance(summary_detail, dict)
        else []
    )
    total_meals = summary.get("total_meals", 0)
    if exceeded:
        return (
            f"오늘 식사 {total_meals}건 기준으로 "
            f"{', '.join(exceeded)} 섭취량이 기준을 초과했습니다."
        )
    return (
        f"오늘 식사 {total_meals}건 기준으로 "
        "현재 초과 항목은 없습니다."
    )


def _summarize_meal_list(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "식사 목록을 조회하지 못했습니다."
        )
    return (
        f"해당 날짜에 기록된 식사는 "
        f"{last.response.get('total', 0)}건입니다."
    )


def _summarize_food_search(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "음식 후보를 찾지 못했습니다."
        )
    candidates = _response_list(last, "candidates")
    if not candidates:
        return (
            "일치하는 음식 후보를 찾지 못했습니다. "
            "음식명을 조금 더 구체적으로 알려주세요."
        )
    names = [
        str(item.get("food_name"))
        for item in candidates[:3]
        if isinstance(item, dict) and item.get("food_name")
    ]
    return "음식 후보를 찾았습니다: " + ", ".join(names)


def _summarize_preference_write(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "영양 선호도를 저장하지 못했습니다."
        )
    fact = _response_dict(last, "fact")
    label = fact.get("object_label") or "해당 항목"
    predicate = (
        fact.get("predicate_label") or fact.get("predicate") or "선호도"
    )
    return f"{label}에 대한 {predicate} 정보를 저장했습니다."


def _summarize_recommendations(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "식단 추천을 처리하지 못했습니다."
        )
    recommendations = _response_list(last, "recommendations")
    if not recommendations:
        return (
            "현재 조건에 맞는 음식을 찾지 못했습니다. 제약 조건을 "
            "조정하거나 다른 음식을 검색해보세요."
        )
    names = [
        str(item.get("food_name", ""))
        for item in recommendations[:3]
        if isinstance(item, dict) and item.get("food_name")
    ]
    text = (
        f"조건에 맞는 음식 {len(recommendations)}가지를 찾았습니다: "
        f"{', '.join(names)}"
    )
    blocked = last.response.get("blocked_count", 0)
    if blocked:
        text += f" (선호도 제한으로 {blocked}가지 제외됨)"
    return text


def _summarize_preference_summary(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    _fallback: str,
) -> str:
    if last.status != "success":
        return _safe_result_error(last.error) or (
            "영양 선호도를 조회하지 못했습니다."
        )
    preferences = _response_dict(last, "preferences")
    counts = preferences.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    return (
        f"저장된 영양 선호도는 제한 {counts.get('hard', 0)}건, "
        f"선호/비선호 {counts.get('soft', 0)}건입니다."
    )


def _summarize_policy(
    _results: list[ToolCallResult],
    last: ToolCallResult,
    fallback: str,
) -> str:
    if (
        last.status == "skipped"
        and last.response.get("reason") == "policy_confirmation_required"
    ):
        if last.tool_name == PROPOSE_SYSTEM_POLICY:
            return "시스템 정책 변경 후보를 만들었습니다. 확인 후 반영됩니다."
        return "알림 정책 변경 후보를 만들었습니다. 확인 후 반영됩니다."
    messages = _result_messages(last.response)
    return (
        " / ".join(messages)
        if messages
        else _safe_result_error(last.error) or fallback
    )


def _response_list(result: ToolCallResult, key: str) -> list[Any]:
    value = result.response.get(key)
    return value if isinstance(value, list) else []


def _response_dict(result: ToolCallResult, key: str) -> dict[str, Any]:
    value = result.response.get(key)
    return value if isinstance(value, dict) else {}


_TOOL_RESULT_SUMMARIZERS: dict[str, ToolResultSummarizer] = {
    GET_PRO_CTCAE_QUESTIONNAIRE: _summarize_pro_ctcae,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT: _summarize_side_effect_lookup,
    GET_SIDE_EFFECT_HISTORY: _summarize_side_effect_history,
    GET_MEDICATION_DOSE_STATUS: _summarize_medication_dose_status,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS: _summarize_medication_write,
    CREATE_NUTRITION_MEAL_RECORD: _summarize_meal_write,
    GET_NUTRITION_DAILY_SUMMARY: _summarize_daily_nutrition,
    GET_NUTRITION_MEAL_RECORD_LIST: _summarize_meal_list,
    SEARCH_NUTRITION_FOOD_CANDIDATES: _summarize_food_search,
    UPSERT_NUTRITION_PREFERENCE_FACT: _summarize_preference_write,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES: _summarize_recommendations,
    GET_NUTRITION_PREFERENCE_SUMMARY: _summarize_preference_summary,
    **{tool_name: _summarize_policy for tool_name in POLICY_TOOLS},
}


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
