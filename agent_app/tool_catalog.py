from __future__ import annotations

from typing import Any


class ToolCatalog:
    @staticmethod
    def available_tools_payload() -> list[dict[str, Any]]:
        return [
            {
                "name": "AE_pro_ctcae",
                "title": "PRO-CTCAE Symptom Matcher",
                "description": "PRO-CTCAE Korean workbook에서 환자가 말한 부작용/증상을 매칭하고 자기보고식 질문과 응답 선택지를 반환합니다.",
                "required_arguments": ["symptom_text", "symptom_normalize"],
                "optional_arguments": ["threshold"],
                "inputSchema": _object_schema(
                    {
                        "symptom_text": {"type": "string", "description": "환자가 말한 원문 증상"},
                        "symptom_normalize": {"type": "string", "description": "정규화된 증상명"},
                        "threshold": {"type": "number", "description": "매칭 임계값"},
                    },
                    ["symptom_text", "symptom_normalize"],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "lookup_side_effect_info",
                "title": "PHR Side Effect Lookup",
                "description": "PHR patient key로 복용 중인 품목의 주의사항을 조회해 증상/부작용 가능성을 평가합니다.",
                "required_arguments": ["symptom_text"],
                "optional_arguments": ["medication_name", "recent_chat", "dose_event_id"],
                "inputSchema": _object_schema(
                    {
                        "symptom_text": {"type": "string", "description": "환자가 말한 증상"},
                        "medication_name": {"type": "string", "description": "선택적 복용 품목명"},
                        "recent_chat": {"type": "array", "items": {"type": "object"}, "description": "최근 대화 맥락"},
                        "dose_event_id": {"type": "integer", "description": "관련 복약 이벤트 ID"},
                    },
                    ["symptom_text"],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "mark_dose_taken",
                "title": "Mark Dose Taken",
                "description": "환자가 이미 복용했음을 명확히 말했을 때 dose_event_id를 taken으로 표시합니다.",
                "required_arguments": ["dose_event_id"],
                "optional_arguments": ["taken_at", "reason"],
                "inputSchema": _object_schema(
                    {
                        "dose_event_id": {"type": "integer", "description": "taken 처리할 복약 이벤트 ID"},
                        "taken_at": {"type": "string", "description": "선택적 복용 완료 시각"},
                        "reason": {"type": "string", "description": "복용 완료 처리 이유"},
                    },
                    ["dose_event_id"],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "apply_notification_policy",
                "title": "Create Notification Policy Candidate",
                "description": (
                    "복약 알림 정책을 적용합니다. 알림 횟수/간격, 기본 알림 시점, 미복용 판단 시간과 알림 문구 템플릿을 포함할 수 있습니다. "
                    "source는 pattern_analysis, patient_request, system_request 중 하나만 사용합니다."
                ),
                "required_arguments": ["slot_label", "extra_reminders", "interval_minutes", "effective_start_date", "effective_end_date", "reason", "source"],
                "optional_arguments": [
                    "missed_dose_after_minutes",
                    "primary_reminder_timing",
                    "primary_reminder_offset_minutes",
                    "medication_title_template",
                    "medication_body_template",
                    "extra_title_template",
                    "extra_body_template",
                    "missed_dose_title_template",
                    "missed_dose_body_template",
                ],
                "inputSchema": _object_schema(
                    {
                        "slot_label": {"type": "string"},
                        "extra_reminders": {"type": "integer"},
                        "interval_minutes": {"type": "integer"},
                        "effective_start_date": {"type": "string"},
                        "effective_end_date": {"type": "string"},
                        "reason": {"type": "string"},
                        "source": {"type": "string", "enum": ["pattern_analysis", "patient_request", "system_request"]},
                        "policies": {"type": "array", "items": {"type": "object"}},
                    },
                    ["slot_label", "extra_reminders", "interval_minutes", "effective_start_date", "effective_end_date", "reason", "source"],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "apply_system_policy",
                "title": "Create System Policy Candidate",
                "description": "복약 알림 정책이 아닌 시스템 운영 정책을 변경합니다. 현재는 daily_pattern_conversation_time 변경을 지원합니다.",
                "required_arguments": ["policy_key", "value", "reason"],
                "optional_arguments": ["source"],
                "inputSchema": _object_schema(
                    {
                        "policy_key": {"type": "string", "enum": ["daily_pattern_conversation_time"]},
                        "value": {"type": "string"},
                        "reason": {"type": "string"},
                        "source": {"type": "string"},
                    },
                    ["policy_key", "value", "reason"],
                ),
                "outputSchema": _tool_result_schema(),
            },
        ]

    @staticmethod
    def tools_for(*names: str) -> list[dict[str, Any]]:
        allowed = set(names)
        return [tool for tool in ToolCatalog.available_tools_payload() if tool["name"] in allowed]


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }


def _tool_result_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "tool_name": {"type": "string"},
            "status": {"type": "string", "enum": ["success", "error", "skipped"]},
            "response": {"type": "object"},
            "error": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        ["tool_name", "status"],
    )
