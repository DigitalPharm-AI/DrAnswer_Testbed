from __future__ import annotations

from typing import Any

from agent_app.tool_side_effects import side_effect_pro_ctcae_summary
from shared.schemas import ToolCallResult


def tool_calls_payload(tool_calls: list[dict[str, Any]], results: list[ToolCallResult]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_calls": tool_calls,
        "tool_results": [result.model_dump(mode="json") for result in results],
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
        return str(last.response.get("message") or last.error or fallback)
    if last.tool_name in {"apply_notification_policy", "apply_system_policy"}:
        if last.status == "skipped" and last.response.get("reason") == "policy_confirmation_required":
            if last.tool_name == "apply_system_policy":
                return "시스템 정책 변경 후보를 만들었습니다. 확인 후 반영됩니다."
            return "알림 정책 변경 후보를 만들었습니다. 확인 후 반영됩니다."
        messages = _result_messages(last.response)
        return " / ".join(messages) if messages else last.error or fallback
    return last.error or fallback


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
