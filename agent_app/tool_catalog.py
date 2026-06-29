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
                "name": "search_food_nutrition",
                "title": "Search Food Nutrition",
                "description": "음식명으로 샘플 음식 영양 후보를 검색합니다. 식사 기록 전에 음식명이 불명확하거나 후보 확인이 필요할 때 사용합니다.",
                "required_arguments": ["query"],
                "optional_arguments": ["limit"],
                "inputSchema": _object_schema(
                    {
                        "query": {"type": "string", "description": "검색할 음식명"},
                        "limit": {"type": "integer", "description": "최대 후보 개수"},
                    },
                    ["query"],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "record_meal",
                "title": "Record Nutrition Meal",
                "description": (
                    "환자가 먹은 음식이 충분히 명확할 때 식사 기록을 저장하고 오늘 영양 요약을 갱신합니다. "
                    "meal_type은 breakfast, lunch, dinner, snack 중 하나입니다."
                ),
                "required_arguments": ["meal_type", "foods"],
                "optional_arguments": ["patient_id", "meal_date", "meal_time", "description"],
                "inputSchema": _object_schema(
                    {
                        "patient_id": {"type": "string", "description": "대상 환자 ID, 생략하면 기본 시뮬레이션 환자"},
                        "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                        "meal_date": {"type": "string", "description": "YYYY-MM-DD, 생략하면 시뮬레이션 현재 날짜"},
                        "meal_time": {"type": "string", "description": "HH:MM 또는 HH:MM:SS, 생략하면 시뮬레이션 현재 시각"},
                        "description": {"type": "string"},
                        "foods": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "food_name": {"type": "string"},
                                    "portion": {"type": "string"},
                                    "nutrients": {"type": "object"},
                                },
                                "required": ["food_name", "nutrients"],
                                "additionalProperties": True,
                            },
                        },
                    },
                    ["meal_type", "foods"],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "list_meals",
                "title": "List Nutrition Meals",
                "description": "특정 날짜 또는 오늘 기록된 식사 목록을 조회합니다.",
                "required_arguments": [],
                "optional_arguments": ["patient_id", "meal_date"],
                "inputSchema": _object_schema(
                    {
                        "patient_id": {"type": "string", "description": "대상 환자 ID, 생략하면 기본 시뮬레이션 환자"},
                        "meal_date": {"type": "string", "description": "YYYY-MM-DD, 생략하면 시뮬레이션 현재 날짜"},
                    },
                    [],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_daily_nutrition_summary",
                "title": "Get Daily Nutrition Summary",
                "description": "오늘 또는 특정 날짜의 영양 섭취량, 기준치, 남은량, 초과 항목을 조회합니다.",
                "required_arguments": [],
                "optional_arguments": ["patient_id", "meal_date"],
                "inputSchema": _object_schema(
                    {
                        "patient_id": {"type": "string", "description": "대상 환자 ID, 생략하면 기본 시뮬레이션 환자"},
                        "meal_date": {"type": "string", "description": "YYYY-MM-DD, 생략하면 시뮬레이션 현재 날짜"},
                    },
                    [],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "record_nutrition_preference",
                "title": "Record Nutrition Preference",
                "description": (
                    "환자가 명시적으로 말한 음식 선호, 비선호, 알레르기, 의학적/종교적 제한을 ontology preference fact로 저장합니다. "
                    "추측하지 말고 사용자 발화에 근거가 있을 때만 사용합니다."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": ["predicate", "object_label"],
                "optional_arguments": ["patient_id", "object_type", "strength", "safety_level", "confidence", "evidence_text"],
                "inputSchema": _object_schema(
                    {
                        "patient_id": {"type": "string", "description": "대상 환자 ID, 생략하면 현재 시뮬레이션 환자"},
                        "predicate": {
                            "type": "string",
                            "enum": [
                                "likes",
                                "dislikes",
                                "prefers",
                                "avoids_by_preference",
                                "allergic_to",
                                "medically_avoids",
                                "religious_avoids",
                            ],
                        },
                        "object_label": {"type": "string", "description": "선호/제한 대상 음식, 재료, 음식군, 식단 유형"},
                        "object_type": {
                            "type": "string",
                            "enum": ["food", "ingredient", "food_category", "cuisine", "preparation", "nutrient", "nutrient_risk", "restriction", "diet_style"],
                        },
                        "strength": {"type": "number", "description": "0.0-1.0 선호 강도"},
                        "safety_level": {"type": "string", "enum": ["hard", "soft"]},
                        "confidence": {"type": "number", "description": "0.0-1.0 근거 신뢰도"},
                        "evidence_text": {"type": "string", "description": "사용자가 말한 원문 근거"},
                    },
                    ["predicate", "object_label"],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_nutrition_preferences",
                "title": "Get Nutrition Preferences",
                "description": "환자별 영양 선호도 ontology summary를 조회합니다. 추천 전 제한/선호 확인이 필요할 때 사용합니다.",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": [],
                "optional_arguments": ["patient_id"],
                "inputSchema": _object_schema(
                    {
                        "patient_id": {"type": "string", "description": "대상 환자 ID, 생략하면 현재 시뮬레이션 환자"},
                    },
                    [],
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "apply_notification_policy",
                "title": "Create Notification Policy Candidate",
                "description": (
                    "복약 알림 정책 변경 후보를 생성합니다. 이 도구는 정책을 직접 적용하지 않고, "
                    "환자 또는 운영자 확인이 필요한 deferred confirmation 결과만 반환합니다. "
                    "알림 횟수/간격, 기본 알림 시점, 미복용 판단 시간과 알림 문구 템플릿을 포함할 수 있습니다. "
                    "source는 pattern_analysis, patient_request, system_request 중 하나만 사용합니다."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
                "_meta": {
                    "execution_mode": "deferred_confirmation",
                    "requires_human_handoff": True,
                    "handoff_gate": "high_risk_policy_change",
                    "approval_actor": "patient_or_operator",
                    "side_effect_level": "approval_required",
                    "risk": "high",
                },
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
                "description": (
                    "복약 알림 정책이 아닌 시스템 운영 정책 변경 후보를 생성합니다. 이 도구는 정책을 직접 적용하지 않고, "
                    "환자 또는 운영자 확인이 필요한 deferred confirmation 결과만 반환합니다. 현재는 daily_pattern_conversation_time 변경을 지원합니다."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
                "_meta": {
                    "execution_mode": "deferred_confirmation",
                    "requires_human_handoff": True,
                    "handoff_gate": "high_risk_policy_change",
                    "approval_actor": "patient_or_operator",
                    "side_effect_level": "approval_required",
                    "risk": "high",
                },
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
