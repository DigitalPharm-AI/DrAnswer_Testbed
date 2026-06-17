from __future__ import annotations

from typing import Any


def async_continuation_type(tool_calls: list[dict[str, Any]]) -> str:
    names = {str(call.get("name") or "") for call in tool_calls}
    if names.intersection({"lookup_side_effect_info", "AE_pro_ctcae"}):
        return "side_effect_assessment"
    if names.intersection({"apply_notification_policy", "apply_system_policy"}):
        return "policy_change_request"
    return ""


def async_continuation_summary(continuation_type: str) -> str:
    if continuation_type == "side_effect_assessment":
        return "증상 내용을 확인해서 문항을 준비할게요."
    if continuation_type == "policy_change_request":
        return "알림 정책 변경 후보를 만들고 있어요. 준비되면 확인할 수 있게 보여드릴게요."
    return ""
