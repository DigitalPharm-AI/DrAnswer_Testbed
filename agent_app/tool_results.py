from __future__ import annotations

from typing import Any

from agent_app.tool_side_effects import side_effect_pro_ctcae_summary
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
    for result in results:
        if result.tool_name == "AE_pro_ctcae" and result.status == "success":
            payload["ae_pro_ctcae"] = result.response
        elif result.tool_name == "lookup_side_effect_info":
            payload["side_effect_lookup"] = result.model_dump(mode="json")
            payload["side_effect_status"] = _side_effect_status(result)
        elif result.tool_name == "mark_dose_taken" and result.status == "success":
            payload["dose_taken_result"] = result.response
        elif result.tool_name == "record_meal" and result.status == "success":
            payload["nutrition_meal_result"] = result.response
        elif result.tool_name == "get_daily_nutrition_summary" and result.status == "success":
            payload["nutrition_daily_summary"] = result.response.get("daily_summary")
        elif result.tool_name == "list_meals" and result.status == "success":
            payload["nutrition_meals"] = result.response.get("meals", [])
        elif result.tool_name == "search_food_nutrition" and result.status == "success":
            search_entry = {
                "query": result.response.get("query", ""),
                "candidates": result.response.get("candidates", []),
            }
            if "food_searches" not in payload:
                payload["food_searches"] = []
            payload["food_searches"].append(search_entry)
            # 단일 검색 호환성 유지 (마지막 검색 결과)
            payload["food_candidates"] = result.response.get("candidates", [])
        elif result.tool_name == "record_nutrition_preference" and result.status == "success":
            payload["nutrition_preference_result"] = result.response
        elif result.tool_name == "get_nutrition_preferences" and result.status == "success":
            payload["nutrition_preferences"] = result.response.get("preferences", {})
        elif result.tool_name == "recommend_diet" and result.status == "success":
            payload["diet_recommendations"] = result.response.get("recommendations", [])
        elif result.tool_name == "apply_notification_policy" and result.status == "success":
            payload["policy_apply_result"] = result.response
        elif result.tool_name == "apply_system_policy" and result.status == "success":
            payload["system_policy_apply_result"] = result.response
    return payload


def tool_result_summary(results: list[ToolCallResult], fallback: str) -> str:
    if not results:
        return fallback
    last = results[-1]
    if last.tool_name == "AE_pro_ctcae":
        if last.status == "success" and last.response.get("matched"):
            side_effect_summary = side_effect_pro_ctcae_summary(results)
            if side_effect_summary:
                return side_effect_summary
            return "증상에 맞는 PRO-CTCAE 자기보고 문항을 준비했습니다."
        if last.status == "success":
            return "정확히 맞는 PRO-CTCAE 증상명은 없어서 그 외 증상 문항으로 확인하겠습니다."
        return "PRO-CTCAE 문항을 불러오지 못했습니다. 증상이 심하거나 지속되면 의료진 또는 약사에게 확인하세요."
    if last.tool_name == "lookup_side_effect_info":
        if last.status == "success" and last.response.get("suspected"):
            return "PHR 주의사항 조회 결과 복용 중인 품목과 관련 가능성이 확인되었습니다."
        if last.status == "success":
            return "현재 PHR 기준으로 직접 일치하는 대표 부작용은 확인되지 않았습니다."
        return "부작용 정보를 조회하지 못했습니다. 증상이 심하거나 지속되면 의료진 또는 약사에게 확인하세요."
    if last.tool_name == "mark_dose_taken":
        return str(last.response.get("message") or _safe_result_error(last.error) or fallback)
    if last.tool_name == "record_meal":
        if last.status != "success":
            return _safe_result_error(last.error) or "식사 기록을 처리하지 못했습니다."
        meal = last.response.get("meal") if isinstance(last.response.get("meal"), dict) else {}
        summary = last.response.get("daily_summary") if isinstance(last.response.get("daily_summary"), dict) else {}
        exceeded = summary.get("summary", {}).get("exceeded_nutrients") if isinstance(summary.get("summary"), dict) else []
        meal_label = meal.get("meal_label") or meal.get("meal_type") or "식사"
        if exceeded:
            return f"{meal_label} 식사를 기록했습니다. 오늘 기준 초과 항목은 {', '.join(exceeded)}입니다."
        return f"{meal_label} 식사를 기록했습니다. 현재까지 초과 항목은 없습니다."
    if last.tool_name == "get_daily_nutrition_summary":
        if last.status != "success":
            return _safe_result_error(last.error) or "오늘 영양 요약을 조회하지 못했습니다."
        summary = last.response.get("daily_summary") if isinstance(last.response.get("daily_summary"), dict) else {}
        total_meals = summary.get("total_meals", 0)
        exceeded = summary.get("summary", {}).get("exceeded_nutrients") if isinstance(summary.get("summary"), dict) else []
        if exceeded:
            return f"오늘 식사 {total_meals}건 기준으로 {', '.join(exceeded)} 섭취량이 기준을 초과했습니다."
        return f"오늘 식사 {total_meals}건 기준으로 현재 초과 항목은 없습니다."
    if last.tool_name == "list_meals":
        if last.status != "success":
            return _safe_result_error(last.error) or "식사 목록을 조회하지 못했습니다."
        total = last.response.get("total", 0)
        return f"해당 날짜에 기록된 식사는 {total}건입니다."
    if last.tool_name == "search_food_nutrition":
        if last.status != "success":
            return _safe_result_error(last.error) or "음식 후보를 찾지 못했습니다."
        candidates = last.response.get("candidates") if isinstance(last.response.get("candidates"), list) else []
        if not candidates:
            return "일치하는 음식 후보를 찾지 못했습니다. 음식명을 조금 더 구체적으로 알려주세요."
        names = [str(item.get("food_name")) for item in candidates[:3] if isinstance(item, dict) and item.get("food_name")]
        return "음식 후보를 찾았습니다: " + ", ".join(names)
    if last.tool_name == "record_nutrition_preference":
        if last.status != "success":
            return _safe_result_error(last.error) or "영양 선호도를 저장하지 못했습니다."
        fact = last.response.get("fact") if isinstance(last.response.get("fact"), dict) else {}
        label = fact.get("object_label") or "해당 항목"
        predicate = fact.get("predicate_label") or fact.get("predicate") or "선호도"
        return f"{label}에 대한 {predicate} 정보를 저장했습니다."
    if last.tool_name == "recommend_diet":
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
    if last.tool_name == "get_nutrition_preferences":
        if last.status != "success":
            return _safe_result_error(last.error) or "영양 선호도를 조회하지 못했습니다."
        preferences = last.response.get("preferences") if isinstance(last.response.get("preferences"), dict) else {}
        counts = preferences.get("counts") if isinstance(preferences.get("counts"), dict) else {}
        return f"저장된 영양 선호도는 제한 {counts.get('hard', 0)}건, 선호/비선호 {counts.get('soft', 0)}건입니다."
    if last.tool_name in {"apply_notification_policy", "apply_system_policy"}:
        if last.status == "skipped" and last.response.get("reason") == "policy_confirmation_required":
            if last.tool_name == "apply_system_policy":
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


def _safe_tool_result_payload(result: ToolCallResult) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    if result.status == "error":
        payload["response"] = redact_for_logging(result.response)
        payload["error"] = _safe_result_error(result.error)
    return payload
