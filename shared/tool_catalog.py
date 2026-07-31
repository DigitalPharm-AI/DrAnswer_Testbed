from __future__ import annotations

from copy import deepcopy
from typing import Any

from shared.tool_names import (
    BACKEND_V13_POLICY_WRITE_TOOLS,
    BACKEND_V13_RECORD_WRITE_TOOLS,
    CHANGE_NOTIFICATION_POLICY,
    GET_NOTIFICATION_POLICIES,
    MODEL_VISIBLE_TOOL_METADATA,
    RECORD_APPROVAL_ACTIONS,
    REQUEST_RECORD_APPROVAL,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)


class ToolCatalog:
    @staticmethod
    def available_tools_payload() -> list[dict[str, Any]]:
        tools = [
            {
                "name": "get_pro_ctcae_questionnaire",
                "title": "PRO-CTCAE Symptom Matcher",
                "description": (
                    "환자가 말한 부작용/증상 원문만 입력받습니다. "
                    "AI Server Tool이 증상 정규화와 서버 설정 임계값을 적용해 PRO-CTCAE Korean workbook을 매칭하고 "
                    "자기보고식 질문과 응답 선택지를 반환합니다."
                ),
                "required_arguments": ["symptom_text"],
                "optional_arguments": [],
                "inputSchema": _object_schema(
                    {
                        "symptom_text": {"type": "string", "description": "환자가 말한 원문 증상"},
                    },
                    ["symptom_text"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_medication_side_effect_assessment",
                "title": "Medication Side Effect Assessment",
                "description": (
                    "환자가 표현한 증상과 약 이름·발생 시점만 입력받습니다. "
                    "환자 식별자와 현재 복약정보는 AI Server가 신뢰 컨텍스트에서 주입하고, "
                    "Tool이 약 후보 매칭과 의약품 부작용 기준정보 조회를 수행합니다."
                ),
                "required_arguments": ["symptom_text"],
                "optional_arguments": ["medication_name", "symptom_onset_text"],
                "inputSchema": _object_schema(
                    {
                        "symptom_text": {
                            "type": "string",
                            "description": "환자가 말한 증상 원문",
                        },
                        "medication_name": {
                            "type": "string",
                            "description": "환자가 직접 언급했거나 대화에서 명확히 지칭한 약 이름",
                        },
                        "symptom_onset_text": {
                            "type": "string",
                            "description": "어제, 복용 30분 후 등 환자가 표현한 증상 발생 시점",
                        },
                    },
                    ["symptom_text"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "create_medication_side_effect_record",
                "title": "Create Medication Side Effect Record",
                "description": "Create a side-effect assessment record only after the assessment is complete and the app confirmation boundary can be applied.",
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": ["symptom_text"],
                "optional_arguments": [
                    "medication_name",
                    "symptom_onset_text",
                ],
                "inputSchema": _object_schema(
                    {
                        "symptom_text": {
                            "type": "string",
                            "description": "환자가 말한 증상 원문",
                        },
                        "medication_name": {
                            "type": "string",
                            "description": "환자가 직접 언급했거나 대화에서 명확히 지칭한 약 이름",
                        },
                        "symptom_onset_text": {
                            "type": "string",
                            "description": "환자가 표현한 증상 발생 시점",
                        },
                    },
                    ["symptom_text"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_side_effect_history",
                "title": "Get Side Effect History",
                "description": "Read previously recorded side-effect assessment records for the current patient. Use target_date for one day, or start_date and end_date for a date range.",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": [],
                "optional_arguments": ["target_date", "start_date", "end_date", "limit", "suspected", "medication_name"],
                "inputSchema": _object_schema(
                    {
                        "target_date": {"type": "string", "description": "YYYY-MM-DD single-day filter using record created_at."},
                        "start_date": {"type": "string", "description": "YYYY-MM-DD range start using record created_at. Use with end_date."},
                        "end_date": {"type": "string", "description": "YYYY-MM-DD range end using record created_at. Use with start_date."},
                        "limit": {"type": "integer", "description": "Maximum records to return. Default 20, maximum 100."},
                        "suspected": {"type": "boolean", "description": "When provided, filter by suspected side-effect status."},
                        "medication_name": {"type": "string", "description": "Optional exact medication name filter."},
                    },
                    [],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_medication_dose_status",
                "title": "Get Medication Dose Status",
                "description": "Read scheduled/taken/missed dose events for a patient. Use target_date for one day, or start_date and end_date for a date range.",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": [],
                "optional_arguments": ["target_date", "start_date", "end_date", "status", "medication_name"],
                "inputSchema": _object_schema(
                    {
                        "target_date": {"type": "string", "description": "YYYY-MM-DD. Omit to use the simulation clock date."},
                        "start_date": {"type": "string", "description": "YYYY-MM-DD range start. Use with end_date."},
                        "end_date": {"type": "string", "description": "YYYY-MM-DD range end. Use with start_date."},
                        "status": {"type": "string", "enum": ["scheduled", "taken", "missed"], "description": "Optional dose status filter."},
                        "medication_name": {"type": "string", "description": "Optional exact medication name filter."},
                    },
                    [],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "update_medication_dose_event_status",
                "title": "Mark Dose Taken",
                "description": "환자가 이미 복용했음을 명확히 말했을 때 dose_event_id를 taken으로 표시합니다.",
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": ["dose_event_id"],
                "optional_arguments": [],
                "inputSchema": _object_schema(
                    {
                        "dose_event_id": {
                            "type": "string",
                            "description": "taken 처리할 복약 이벤트의 공개 opaque ID",
                        },
                    },
                    ["dose_event_id"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "search_nutrition_food_candidates",
                "title": "Search Food Nutrition",
                "description": "한 식사에서 사용자가 말한 음식명 목록을 한 번에 검색하고 음식별 영양 후보 그룹을 반환합니다. 식사 기록 전에 음식명이 불명확하거나 후보 확인이 필요할 때 사용합니다.",
                "required_arguments": ["food_queries"],
                "optional_arguments": ["limit_per_query", "meal_type"],
                "inputSchema": _object_schema(
                    {
                        "food_queries": {
                            "type": "array",
                            "description": "사용자 발화에 명시된 서로 다른 음식명 목록. 복합 음식은 재료로 나누지 않습니다.",
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 100,
                            },
                            "minItems": 1,
                            "maxItems": 8,
                            "uniqueItems": True,
                        },
                        "limit_per_query": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "description": "음식명별 최대 후보 개수. 기본값은 6입니다.",
                        },
                        "meal_type": {
                            "type": "string",
                            "enum": ["breakfast", "lunch", "dinner", "snack"],
                            "description": "사용자 발화에 아침/점심/저녁/간식 식사 종류가 명확할 때 UI 기본 선택값으로 전달합니다.",
                        },
                    },
                    ["food_queries"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "create_nutrition_meal_record",
                "title": "Record Nutrition Meal",
                "description": ("환자가 먹은 음식이 충분히 명확할 때 식사 기록을 저장하고 오늘 영양 요약을 갱신합니다. meal_type은 breakfast, lunch, dinner, snack 중 하나입니다."),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": ["meal_type", "foods"],
                "optional_arguments": ["meal_date", "meal_time", "description"],
                "inputSchema": _object_schema(
                    {
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
                                    "nutrients": _nutrient_schema(),
                                },
                                "required": ["food_name", "nutrients"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    ["meal_type", "foods"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "update_nutrition_meal_record",
                "title": "Update Nutrition Meal",
                "description": (
                    "Update an existing meal record while keeping the same meal_id. Use only when the target meal_id is clear. "
                    "Provide one or more fields to update: meal_type, meal_date, meal_time, description, scenario_key, or foods. "
                    "When foods is provided it replaces the meal food list."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "_meta": {
                    "domain": "nutrition",
                    "source_repo": "DrAnswer_Testbed",
                    "source_path": "system_app/routes/agent_api.py",
                    "source_tool_name": "agent_nutrition_update_meal",
                    "mutability": "write",
                    "risk_level": "medium",
                },
                "required_arguments": ["meal_id"],
                "optional_arguments": ["meal_type", "meal_date", "meal_time", "scenario_key", "description", "foods"],
                "inputSchema": _object_schema(
                    {
                        "meal_id": {
                            "type": "string",
                            "description": "Existing meal public opaque ID to update",
                        },
                        "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                        "meal_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "meal_time": {"type": "string", "description": "HH:MM or HH:MM:SS"},
                        "scenario_key": {"type": "string"},
                        "description": {"type": "string"},
                        "foods": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "food_name": {"type": "string"},
                                    "portion": {"type": "string"},
                                    "nutrients": _nutrient_schema(),
                                },
                                "required": ["food_name", "nutrients"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    ["meal_id"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "delete_nutrition_meal_record",
                "title": "Delete Nutrition Meal",
                "description": "Delete an existing meal record by meal_id. Use only after the target meal is clear or confirmed.",
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "_meta": {
                    "domain": "nutrition",
                    "source_repo": "DrAnswer_Testbed",
                    "source_path": "system_app/routes/agent_api.py",
                    "source_tool_name": "agent_nutrition_delete_meal",
                    "mutability": "delete",
                    "risk_level": "medium",
                },
                "required_arguments": ["meal_id"],
                "optional_arguments": [],
                "inputSchema": _object_schema(
                    {
                        "meal_id": {
                            "type": "string",
                            "description": "Existing meal public opaque ID to delete",
                        },
                    },
                    ["meal_id"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "update_nutrition_food_record",
                "title": "Update Nutrition Food",
                "description": (
                    "Update or replace one food item inside an existing meal using meal_id and food_id. "
                    "Use after list_meals identifies the target food. If replacing the food with another food, "
                    "use search_food_nutrition first when the new food's nutrients are not already clear."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "_meta": {
                    "domain": "nutrition",
                    "source_repo": "DrAnswer_Testbed",
                    "source_path": "system_app/routes/agent_api.py",
                    "source_tool_name": "agent_nutrition_update_food",
                    "mutability": "write",
                    "risk_level": "medium",
                },
                "required_arguments": ["meal_id", "food_id"],
                "optional_arguments": ["food_ref_id", "food_name", "portion", "nutrients"],
                "inputSchema": _object_schema(
                    {
                        "meal_id": {
                            "type": "string",
                            "description": "Existing meal public opaque ID containing the food",
                        },
                        "food_id": {
                            "type": "string",
                            "description": "Existing food public opaque ID to update",
                        },
                        "food_ref_id": {"type": "string", "description": "Reference id for the replacement food when known"},
                        "food_name": {"type": "string", "description": "Updated or replacement food name"},
                        "portion": {"type": "string", "description": "Updated portion text"},
                        "nutrients": _nutrient_schema(),
                    },
                    ["meal_id", "food_id"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "delete_nutrition_food_record",
                "title": "Delete Nutrition Food",
                "description": (
                    "Delete one food item from an existing meal using meal_id and food_id. "
                    "Use after list_meals identifies the target food. If the meal becomes empty, the meal is deleted by default."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "_meta": {
                    "domain": "nutrition",
                    "source_repo": "DrAnswer_Testbed",
                    "source_path": "system_app/routes/agent_api.py",
                    "source_tool_name": "agent_nutrition_delete_food",
                    "mutability": "delete",
                    "risk_level": "medium",
                },
                "required_arguments": ["meal_id", "food_id"],
                "optional_arguments": [],
                "inputSchema": _object_schema(
                    {
                        "meal_id": {
                            "type": "string",
                            "description": "Existing meal public opaque ID containing the food",
                        },
                        "food_id": {
                            "type": "string",
                            "description": "Existing food public opaque ID to delete",
                        },
                    },
                    ["meal_id", "food_id"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_nutrition_meal_record_list",
                "title": "List Nutrition Meals",
                "description": "특정 날짜 또는 오늘 기록된 식사 목록을 조회합니다.",
                "required_arguments": [],
                "optional_arguments": ["meal_date"],
                "inputSchema": _object_schema(
                    {
                        "meal_date": {"type": "string", "description": "YYYY-MM-DD, 생략하면 시뮬레이션 현재 날짜"},
                    },
                    [],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_nutrition_daily_summary",
                "title": "Get Daily Nutrition Summary",
                "description": "오늘 또는 특정 날짜의 영양 섭취량, 기준치, 남은량, 초과 항목을 조회합니다.",
                "required_arguments": [],
                "optional_arguments": ["meal_date"],
                "inputSchema": _object_schema(
                    {
                        "meal_date": {"type": "string", "description": "YYYY-MM-DD, 생략하면 시뮬레이션 현재 날짜"},
                    },
                    [],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "upsert_nutrition_preference_fact",
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
                "optional_arguments": ["object_type", "strength", "safety_level", "confidence", "evidence_text"],
                "inputSchema": _object_schema(
                    {
                        "predicate": {
                            "type": "string",
                            "enum": [
                                "likes",
                                "dislikes",
                                "prefers",
                                "avoids_by_preference",
                                "cannot_consume",
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
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_nutrition_preference_summary",
                "title": "Get Nutrition Preferences",
                "description": "환자별 영양 선호도 ontology summary를 조회합니다. 추천 전 제한/선호 확인이 필요할 때 사용합니다.",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": [],
                "optional_arguments": [],
                "inputSchema": _object_schema(
                    {},
                    [],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "get_nutrition_recommendation_candidates",
                "title": "Recommend Diet",
                "description": (
                    "환자의 질환, CKD 위험도, 오늘 섭취 현황을 고려해 영양 제약 조건에 맞는 음식을 추천합니다. "
                    "context.nutrition.today_summary에서 초과 또는 기준치 근접 영양소를 'low'로, "
                    "칼로리가 적절한 범위이면 'moderate'로 constraints에 지정하세요. "
                    "hard_constraints에 있는 음식은 자동으로 제외됩니다. "
                    "식사 기록(record_meal)이 아닌 추천 요청에만 사용합니다."
                ),
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": ["constraints"],
                "optional_arguments": ["meal_type", "limit", "randomize"],
                "inputSchema": _object_schema(
                    {
                        "constraints": {
                            "type": "object",
                            "description": (
                                "영양소별 제약 수준. 키: 나트륨|단백질|칼로리|지방|탄수화물, "
                                "값: low(낮게 유지) 또는 moderate(적정 범위). "
                                '예: {"나트륨": "low", "단백질": "low", "칼로리": "moderate"}'
                            ),
                        },
                        "randomize": {
                            "type": "boolean",
                            "description": "조건에 맞는 후보를 매번 같은 순서가 아닌 랜덤 순서로 추천합니다. 기본값 true.",
                        },
                        "meal_type": {
                            "type": "string",
                            "enum": ["breakfast", "lunch", "dinner", "snack"],
                            "description": "Meal type. breakfast/lunch/dinner prioritizes meal-like foods over snacks or beverages; snack allows snack-like candidates. Optional.",
                        },
                        "limit": {"type": "integer", "description": "최대 추천 개수 (기본 5)"},
                    },
                    ["constraints"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": "propose_notification_policy",
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
                "name": "propose_system_policy",
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
            {
                "name": GET_NOTIFICATION_POLICIES,
                "title": "Get Notification Policies",
                "description": (
                    "현재 환자의 Backend 알림 정책과 공개 policy_id를 조회합니다. "
                    "알림 정책 변경 전에 호출해 대상 정책을 식별합니다."
                ),
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": [],
                "optional_arguments": ["policy_id", "slot_label", "active_only"],
                "inputSchema": _object_schema(
                    {
                        "policy_id": {
                            "type": "string",
                            "description": "Backend가 발급한 공개 알림 정책 ID",
                        },
                        "slot_label": {
                            "type": "string",
                            "description": "정책 대상 복약 시간대",
                        },
                        "active_only": {
                            "type": "boolean",
                            "description": "활성 정책만 조회할지 여부. 기본값 true",
                        },
                    },
                    [],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
            {
                "name": CHANGE_NOTIFICATION_POLICY,
                "title": "Change Notification Policy",
                "description": (
                    "승인 카드에서 사용자가 적용 또는 유지 결정을 확인한 뒤 서버가 강제 실행합니다. "
                    "모델은 이 Tool을 직접 호출하지 않고 승인 요청 Tool을 사용합니다. 승인 후 Tool 내부에서 "
                    "Backend /agent/sync/notification-policy-change API를 동기로 호출합니다."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": ["policy_id", "decision"],
                "optional_arguments": ["changes"],
                "inputSchema": _object_schema(
                    {
                        "policy_id": {"type": "string", "description": "Backend가 발급한 공개 notification policy ID"},
                        "decision": {"type": "string", "enum": ["apply", "keep"]},
                        "changes": _notification_policy_changes_schema(),
                    },
                    ["policy_id", "decision"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            },
        ]
        canonical_tools = [_tool_payload(tool) for tool in tools]
        write_argument_schemas = {
            tool["name"]: deepcopy(tool["inputSchema"])
            for tool in canonical_tools
            if tool["name"] in RECORD_APPROVAL_ACTIONS
        }
        approval_tool = _tool_payload(
            {
                "name": REQUEST_RECORD_APPROVAL,
                "title": "Request Record Approval",
                "description": (
                    "기록 생성·수정·삭제 또는 알림 정책 변경 전에 사용자 승인을 요청합니다. "
                    "이 Tool은 데이터를 직접 변경하지 않으며 승인용 selection box만 준비합니다."
                ),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "required_arguments": ["action_name", "record_arguments"],
                "optional_arguments": [],
                "inputSchema": _object_schema(
                    {
                        "action_name": {
                            "type": "string",
                            "enum": sorted(RECORD_APPROVAL_ACTIONS),
                            "description": "승인 후 실행할 canonical write Tool 이름",
                        },
                        "record_arguments": _record_approval_arguments_schema(
                            write_argument_schemas,
                            RECORD_APPROVAL_ACTIONS,
                        ),
                    },
                    ["action_name", "record_arguments"],
                    additional_properties=False,
                ),
                "outputSchema": _tool_result_schema(),
            }
        )
        return [*canonical_tools, approval_tool]

    @staticmethod
    def tools_for(*names: str) -> list[dict[str, Any]]:
        allowed = set(names)
        return [tool for tool in ToolCatalog.available_tools_payload() if tool["name"] in allowed]

    @staticmethod
    def model_tools_for(*names: str) -> list[dict[str, Any]]:
        """Return the least-privilege Tool catalog exposed to an LLM.

        Raw record-write Tools remain in ``tools_for`` for trusted, server-forced
        execution after a confirmation. When an agent can request record
        approval, however, the model sees only the approval wrapper and the
        wrapper is limited to that agent's own write actions.
        """

        allowed = set(names)
        scoped_actions = allowed.intersection(RECORD_APPROVAL_ACTIONS)
        model_allowed = allowed.difference(RECORD_APPROVAL_ACTIONS)
        if REQUEST_RECORD_APPROVAL not in allowed:
            return ToolCatalog.tools_for(*model_allowed)

        available_tools = ToolCatalog.available_tools_payload()
        tools = [
            tool
            for tool in available_tools
            if tool["name"] in model_allowed
        ]
        if not scoped_actions:
            return [
                tool
                for tool in tools
                if tool["name"] != REQUEST_RECORD_APPROVAL
            ]

        scoped_tools: list[dict[str, Any]] = []
        for tool in tools:
            if tool["name"] != REQUEST_RECORD_APPROVAL:
                scoped_tools.append(tool)
                continue

            approval_tool = deepcopy(tool)
            approval_schema = approval_tool["inputSchema"]
            approval_schema["properties"]["action_name"]["enum"] = sorted(
                scoped_actions
            )
            action_schemas = {
                candidate["name"]: candidate["inputSchema"]
                for candidate in available_tools
                if candidate["name"] in scoped_actions
            }
            approval_schema["properties"][
                "record_arguments"
            ] = _record_approval_arguments_schema(
                action_schemas,
                scoped_actions,
            )
            scoped_tools.append(approval_tool)
        return scoped_tools


def _object_schema(
    properties: dict[str, Any],
    required: list[str],
    *,
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": additional_properties,
    }


def _record_approval_arguments_schema(
    action_schemas: dict[str, dict[str, Any]],
    action_names: set[str] | frozenset[str],
) -> dict[str, Any]:
    """Build a Bedrock-compatible union of the scoped action properties.

    Bedrock Converse rejects ``oneOf``/``allOf``/``anyOf`` at a Tool
    ``inputSchema`` root. The approval wrapper therefore advertises the
    property union for the actions visible to that specialist. Individual
    action required-field and semantic validation remains authoritative at
    the server permission/execution boundary.
    """

    properties: dict[str, Any] = {}
    for action_name in sorted(action_names):
        action_schema = action_schemas.get(action_name)
        if not isinstance(action_schema, dict):
            continue
        action_properties = action_schema.get("properties")
        if not isinstance(action_properties, dict):
            continue
        for property_name, property_schema in action_properties.items():
            properties.setdefault(
                property_name,
                deepcopy(property_schema),
            )

    schema = _object_schema(
        properties,
        [],
        additional_properties=False,
    )
    schema["description"] = (
        "action_name 대상 write Tool의 업무 인자. "
        "action별 필수값과 의미 검증은 AI Server가 수행합니다."
    )
    return schema


def _nutrient_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "calories": {"type": "number", "minimum": 0},
            "protein": {"type": "number", "minimum": 0},
            "sodium": {"type": "number", "minimum": 0},
            "fat": {"type": "number", "minimum": 0},
            "carbohydrates": {"type": "number", "minimum": 0},
        },
        [],
        additional_properties=False,
    )


def _notification_policy_changes_schema() -> dict[str, Any]:
    schema = _object_schema(
        {
            "extra_reminders": {"type": "integer", "minimum": 0, "maximum": 5},
            "interval_minutes": {"type": "integer", "minimum": 5, "maximum": 60},
            "missed_dose_after_minutes": {"type": "integer", "minimum": 15, "maximum": 240},
            "primary_reminder_timing": {
                "type": "string",
                "enum": ["before", "at", "after"],
            },
            "primary_reminder_offset_minutes": {
                "type": "integer",
                "minimum": 0,
                "maximum": 120,
            },
            "effective_start_date": {"type": "string", "format": "date"},
            "effective_end_date": {"type": "string", "format": "date"},
        },
        [],
        additional_properties=False,
    )
    schema["minProperties"] = 1
    schema["description"] = "decision=apply일 때 적용할 변경 필드"
    return schema


def _tool_result_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "tool_name": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["success", "error", "skipped", "confirmation_required"],
            },
            "response": {"type": "object"},
            "error": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        ["tool_name", "status"],
        additional_properties=True,
    )


def _tool_payload(tool: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(tool)
    name = str(payload.get("name") or "")
    payload["name"] = name
    if name == UPSERT_NUTRITION_PREFERENCE_FACT:
        payload["description"] = (
            "Stores an explicitly stated nutrition preference or restriction as an ontology fact. "
            "Use preference predicates only for voluntary likes or dislikes. Use cannot_consume for a hard "
            "ingestion restriction whose cause is not specified, allergic_to only for an explicit allergy, "
            "medically_avoids only for an explicit medical restriction, and religious_avoids only for an "
            "explicit religious restriction. Never store inability to consume as avoids_by_preference."
        )
        predicate_schema = payload.get("inputSchema", {}).get("properties", {}).get("predicate", {})
        predicate_schema["description"] = "Hard restrictions must not use a preference predicate."
    args_schema = payload.get("inputSchema") if isinstance(payload.get("inputSchema"), dict) else _object_schema({}, [])
    payload["args_schema"] = args_schema
    metadata = MODEL_VISIBLE_TOOL_METADATA.get(name, {})
    if name in BACKEND_V13_RECORD_WRITE_TOOLS:
        metadata = {
            **metadata,
            "source_path": "agent_app/tools/backend_write.py",
            "source_tool_name": name,
            "execution_mode": "backend_sync",
            "backend_endpoint": "/agent/sync/record-change",
            "confirmation_policy": "explicit_user_message",
        }
    elif name in BACKEND_V13_POLICY_WRITE_TOOLS:
        metadata = {
            **metadata,
            "execution_mode": "backend_sync",
            "backend_endpoint": "/agent/sync/notification-policy-change",
            "confirmation_policy": "explicit_user_message",
        }
    payload.update(metadata)
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    payload["_meta"] = {**meta, **metadata}
    return payload
