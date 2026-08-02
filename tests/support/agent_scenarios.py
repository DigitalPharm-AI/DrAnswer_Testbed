from __future__ import annotations

from datetime import date, datetime
from typing import Any

from agent_app.tools.policy import (
    deferred_policy_tool_result,
    is_deferred_policy_tool_call,
)
from shared.schemas import (
    DailyMedicationPattern,
    DosePatternEvent,
    MissedDoseEventPayload,
    MultiturnChatRequest,
    SlotAdherenceSummary,
    ToolCallResult,
)
from shared.tool_names import (
    GET_MEDICATION_DOSE_STATUS,
    REQUEST_RECORD_APPROVAL,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)
from shared.tool_permissions import (
    permission_denied_result,
    validate_tool_permission,
)
from tests.support.llm import NativeChatProvider


class NativeDelegatingMedicationProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.bound_tool_names: list[str] = []
        self.bound_tool_history: list[list[str]] = []

    async def model_output(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.seen_payloads.append(user_payload)
        self.bound_tool_history.append(list(self.bound_tool_names))
        if user_payload.get("response_mode") == "multiturn_chat":
            return {
                "message": "복약 담당 에이전트가 확인하겠습니다.",
                "tool_call": {
                    "name": "delegate_to_medication_agent",
                    "arguments": {
                        "task": "record reported dose as taken",
                        "reason": (
                            "patient reported taking a current medication dose"
                        ),
                    },
                },
            }
        if user_payload.get("response_mode") == "medication_chat":
            return {
                "message": "복약 완료를 기록하겠습니다.",
                "tool_call": {
                    "name": REQUEST_RECORD_APPROVAL,
                    "arguments": {
                        "action_name": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
                        "record_arguments": {
                            "dose_event_id": "dose-event-12",
                        },
                    },
                },
            }
        if user_payload.get("response_mode") == "final_answer":
            return {"advice": "복약 요청 처리를 완료했습니다."}
        return {"advice": "확인했습니다."}


class NativeRecentChatProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.seen_prompts: list[str] = []

    async def model_output(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.seen_prompts.append(system_prompt)
        self.seen_payloads.append(user_payload)
        return {
            "advice": (
                "앞서 속이 메스꺼운데 약 때문일지 물어보셨고, "
                "PRO-CTCAE 문항에는 1번 자주 있다, 2번 보통이다로 "
                "답하셨습니다."
            ),
            "observations": [
                "recent_chat을 참고해 일반 대화로 답변했습니다."
            ],
            "tool_calls": [],
        }


class NativeFakeToolExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_tool_call(
        self,
        tool_call: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        name = str(tool_call.get("name"))
        denial_reason = validate_tool_permission(
            tool_call,
            source_event_type=source_event_type,
            payload=payload,
        )
        if denial_reason:
            return permission_denied_result(
                tool_call,
                trace_id=trace_id,
                source_event_type=source_event_type,
                reason=denial_reason,
            )
        if is_deferred_policy_tool_call(tool_call):
            return deferred_policy_tool_result(
                tool_call,
                trace_id=trace_id,
                source_event_type=source_event_type,
            )
        self.calls.append(
            {**tool_call, "_source_event_type": source_event_type}
        )
        arguments = tool_call.get("arguments") or {}
        if name == REQUEST_RECORD_APPROVAL:
            action_name = str(arguments["action_name"])
            return ToolCallResult(
                tool_name=name,
                status="confirmation_required",
                response={
                    "mutation_confirmation": {
                        "confirmation_required": True,
                        "action_type": "agent_tool",
                        "action_name": action_name,
                        "tool_call_id": str(tool_call.get("id") or ""),
                        "action_fingerprint": f"fingerprint-{action_name}",
                        "status": "pending",
                        "display": {
                            "title": "record confirmation",
                            "question": "confirm record change",
                        },
                    }
                },
                idempotency_key=f"{trace_id}:{name}:{action_name}",
            )
        if name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "dose_event_id": arguments["dose_event_id"],
                    "status": "taken",
                    "message": "아침 08:00 혈압약 복약을 완료로 기록했습니다.",
                },
                idempotency_key=(
                    f"{trace_id}:{name}:{source_event_type}:12"
                ),
            )
        if name == GET_MEDICATION_DOSE_STATUS:
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "dose_events": [
                        {
                            "slot_label": "아침 08:00",
                            "status": "taken",
                            "taken_at": "2026-04-20T09:30:00",
                        }
                    ],
                    "total": 1,
                    "totals_by_status": {
                        "taken": 1,
                        "scheduled": 0,
                        "missed": 0,
                    },
                },
                idempotency_key=(
                    f"{trace_id}:get_medication_dose_status"
                ),
            )
        return self._domain_result(
            name=name,
            arguments=arguments,
            trace_id=trace_id,
            source_event_type=source_event_type,
        )

    @staticmethod
    def _domain_result(
        *,
        name: str,
        arguments: dict[str, Any],
        trace_id: str,
        source_event_type: str,
    ) -> ToolCallResult:
        if name == "propose_notification_policy":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "idempotency_key": (
                        f"{trace_id}:{name}:{source_event_type}"
                    ),
                    "results": [
                        {
                            "slot_label": "아침 08:00",
                            "applied": True,
                            "message": "정책을 적용했습니다.",
                        }
                    ],
                    "all_applied": True,
                },
                idempotency_key=f"{trace_id}:{name}:{source_event_type}",
            )
        if name == "get_medication_side_effect_assessment":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "suspected": True,
                    "matched_effects": ["메스꺼움"],
                    "matched_items": ["항암제"],
                    "severity": "moderate",
                    "evidence": "주의사항에 메스꺼움이 포함되어 있습니다.",
                    "recommendation": "증상 문항 확인이 필요합니다.",
                },
                idempotency_key=f"{trace_id}:{name}",
            )
        if name == "get_pro_ctcae_questionnaire":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "input_symptom": arguments["symptom_text"],
                    "matched": True,
                    "match_type": "exact",
                    "matched_symptom_term": "Nausea",
                    "matched_korean_symptom_name": "메스꺼움",
                    "similarity": 1.0,
                    "threshold": 0.56,
                    "scoring_method": "test",
                    "embedding_provider": "",
                    "sheet_name": "Parsed_Items",
                    "questions": [],
                    "candidates": [],
                },
                idempotency_key=f"{trace_id}:{name}",
            )
        if name == "search_nutrition_food_candidates":
            food_queries = arguments.get("food_queries", [])
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "search_groups": [
                        {
                            "query": query,
                            "candidates": [
                                {
                                    "food_ref_id": f"food-{index}",
                                    "food_name": query,
                                    "serving_size": 50,
                                    "nutrients": {
                                        "calories": 70,
                                        "protein": 6,
                                    },
                                }
                            ],
                        }
                        for index, query in enumerate(food_queries)
                    ],
                },
                idempotency_key=f"{trace_id}:{name}",
            )
        if name == "get_nutrition_meal_record_list":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "meals": [
                        {
                            "id": 101,
                            "meal_type": "lunch",
                            "meal_label": "점심",
                            "foods": [
                                {
                                    "id": 202,
                                    "food_ref_id": "tangsuyuk",
                                    "food_name": "탕수육",
                                    "portion": {
                                        "amount": 1,
                                        "unit": "serving",
                                    },
                                    "nutrients": {
                                        "calories": 420,
                                        "protein": 16,
                                    },
                                }
                            ],
                        }
                    ],
                },
                idempotency_key=f"{trace_id}:{name}",
            )
        if name == "upsert_nutrition_preference_fact":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "fact": {
                        "predicate": arguments["predicate"],
                        "object_label": arguments["object_label"],
                    },
                },
                idempotency_key=f"{trace_id}:{name}",
            )
        if name == "get_nutrition_recommendation_candidates":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "recommendations": [
                        {"food_name": "두부 샐러드", "score": 0.9}
                    ],
                    "blocked_count": 0,
                },
                idempotency_key=f"{trace_id}:{name}",
            )
        if name in {
            "update_nutrition_meal_record",
            "delete_nutrition_meal_record",
        }:
            operation = "meal" if name.startswith("update") else "deleted_meal"
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    operation: {
                        "id": arguments["meal_id"],
                        "meal_label": "점심",
                    },
                    "daily_summary": {},
                },
                idempotency_key=(
                    f"{trace_id}:{name}:{arguments['meal_id']}"
                ),
            )
        if name == "update_nutrition_food_record":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "meal": {
                        "id": arguments["meal_id"],
                        "meal_label": "점심",
                    },
                    "food": {
                        "id": arguments["food_id"],
                        "food_name": arguments.get(
                            "food_name", "수정 음식"
                        ),
                    },
                    "daily_summary": {},
                },
                idempotency_key=(
                    f"{trace_id}:{name}:{arguments['meal_id']}:"
                    f"{arguments['food_id']}"
                ),
            )
        if name == "delete_nutrition_food_record":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "meal_id": arguments["meal_id"],
                    "food_id": arguments["food_id"],
                    "deleted_food": {
                        "id": arguments["food_id"],
                        "food_name": "삭제 음식",
                    },
                    "daily_summary": {},
                    "meal_deleted": False,
                },
                idempotency_key=(
                    f"{trace_id}:{name}:{arguments['meal_id']}:"
                    f"{arguments['food_id']}"
                ),
            )
        return ToolCallResult(
            tool_name=name,
            status="error",
            error="unexpected_tool",
        )


def build_daily_pattern() -> DailyMedicationPattern:
    return DailyMedicationPattern(
        patient_id="demo-patient",
        date=date(2026, 4, 20),
        schedule_slots=["아침 08:00"],
        dose_events=[
            DosePatternEvent(
                dose_event_id=12,
                medication_name="혈압약",
                slot_label="아침 08:00",
                scheduled_for=datetime(2026, 4, 20, 8, 0),
                status="missed",
            )
        ],
        slot_summaries=[
            SlotAdherenceSummary(
                slot_label="아침 08:00",
                scheduled_count=1,
                taken_count=0,
                missed_count=1,
                miss_rate=1.0,
            )
        ],
    )


def build_repeated_daily_pattern() -> DailyMedicationPattern:
    return DailyMedicationPattern(
        patient_id="demo-patient",
        date=date(2026, 4, 21),
        window_start_date=date(2026, 4, 20),
        window_end_date=date(2026, 4, 21),
        window_days=2,
        observed_day_count=2,
        schedule_slots=["아침 08:00"],
        dose_events=[
            DosePatternEvent(
                dose_event_id=12,
                medication_name="혈압약",
                slot_label="아침 08:00",
                scheduled_for=datetime(2026, 4, 20, 8, 0),
                status="missed",
            ),
            DosePatternEvent(
                dose_event_id=13,
                medication_name="혈압약",
                slot_label="아침 08:00",
                scheduled_for=datetime(2026, 4, 21, 8, 0),
                status="missed",
            ),
        ],
        slot_summaries=[
            SlotAdherenceSummary(
                slot_label="아침 08:00",
                scheduled_count=2,
                taken_count=0,
                missed_count=2,
                miss_rate=1.0,
            )
        ],
    )


def build_missed_payload() -> MissedDoseEventPayload:
    return MissedDoseEventPayload(
        patient_id="demo-patient",
        dose_event_id=12,
        medication_name="혈압약",
        slot_label="아침 08:00",
        scheduled_for=datetime(2026, 4, 20, 8, 0),
        detected_at=datetime(2026, 4, 20, 9, 30),
        recent_slot_summaries=[],
        adherence_pattern_context={
            "pattern_code": "B",
            "pattern_label": "습관 미형성",
            "reason": (
                "복약 루틴 형성을 위한 단기 미복용 확인이 필요합니다."
            ),
        },
        tone_policy_context={
            "pattern_code": "B",
            "tone_key": "persuasion",
            "message": "복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
            "message_variant": "v1",
            "message_catalog_source": "test_catalog",
        },
        chat_context=[],
    )


def build_taken_chat_request() -> MultiturnChatRequest:
    return MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="아침 약은 방금 복용했어. 기록해줘.",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={
            "schedule_slots": ["아침 08:00"],
            "today_dose_events": [
                {
                    "dose_event_id": "dose-event-12",
                    "medication_name": "혈압약",
                    "slot_label": "아침 08:00",
                    "scheduled_for": "2026-04-20T08:00:00",
                    "status": "missed",
                }
            ],
        },
    )
