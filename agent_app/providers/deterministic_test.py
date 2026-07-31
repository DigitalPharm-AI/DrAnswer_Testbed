from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from agent_app.llm.messages import (
    ai_message_from_tool_calls,
    human_payload_from_messages,
    tool_results_from_messages,
)
from agent_app.llm.validation import validate_llm_output
from agent_app.llm.context import context_value
from agent_app.providers.base import BaseLLMProvider, GenerationReadiness
from agent_app.llm.responses import natural_chat_summary
from agent_app.tools.calling import normalize_tool_calls
from shared.tool_names import (
    DELEGATION_TOOL_NAMES,
    DELEGATE_TO_MEDICATION_AGENT,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    PROPOSE_NOTIFICATION_POLICY,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from agent_app.tools.results import tool_result_summary


class DeterministicTestProvider(BaseLLMProvider):
    """Scripted provider available only in explicit test environments."""

    def configuration_readiness(self) -> GenerationReadiness:
        return GenerationReadiness(ok=True, code="OK")

    def generation_readiness(self) -> GenerationReadiness:
        return GenerationReadiness(ok=True, code="OK")

    def chat_model(self):
        return DeterministicTestChatModel(provider=self)

    def model_output(self, user_payload: dict[str, Any]) -> dict[str, Any]:
        response_mode = user_payload.get("response_mode")
        if response_mode == "daily_pattern_analysis":
            missed_slots = _missed_slots(user_payload)
            return {
                "summary": "미복용 패턴을 결정적 테스트 모델로 분석했습니다.",
                "repeated_missed_slots": missed_slots,
                "tool_calls": [_notification_policy_tool_call(slot_label, user_payload) for slot_label in missed_slots],
            }
        if response_mode in {
            "missed_dose_coaching",
            "missed_dose_message_generation",
        }:
            generated_message = (
                "복약을 놓친 상황을 확인하고 싶어요. "
                "어떤 어려움이 있었나요?"
            )
            if response_mode == "missed_dose_message_generation":
                return {"generated_message": generated_message}
            return {
                "patient_message": generated_message,
                "likely_reason": "아직 이유가 확인되지 않았습니다.",
                "side_effect_signal": False,
                "symptom_summary": "",
                "follow_up_questions": ["현재 복용 가능하신 상태인가요?"],
                "recommendation": "복용 가능 여부와 미복용 이유를 먼저 확인하세요.",
                "missed_dose_hybrid": {
                    "reason": "복용 가능 여부를 먼저 확인하도록 비지시형으로 안내합니다.",
                    "generated_message": generated_message,
                    "pattern_code": "",
                    "tone_key": "",
                    "safety_notes": ["no_medication_name", "no_diagnosis", "non_directive"],
                },
            }
        if response_mode == "medication_chat":
            side_effect_tool_call = _deterministic_side_effect_tool_call(user_payload)
            if side_effect_tool_call:
                return {
                    "advice": "말씀하신 증상을 확인해볼게요.",
                    "tool_calls": [side_effect_tool_call],
                }
            medication_status_tool_call = _deterministic_medication_status_tool_call(user_payload)
            if medication_status_tool_call:
                return {
                    "advice": "복약 일정과 복용 상태를 확인할게요.",
                    "tool_calls": [medication_status_tool_call],
                }
            return {"message": "복약 관련 요청을 확인했습니다."}
        if response_mode == "multiturn_chat":
            side_effect_tool_call = _deterministic_side_effect_tool_call(user_payload)
            if side_effect_tool_call:
                return {
                    "advice": "복약 담당 에이전트가 증상을 확인할게요.",
                    "tool_calls": [
                        {
                            "name": DELEGATE_TO_MEDICATION_AGENT,
                            "arguments": {
                                "task": "assess a possible medication side effect and prepare PRO-CTCAE questions when indicated",
                                "reason": "patient reported a possible medication-related symptom",
                            },
                        }
                    ],
                }
            medication_status_tool_call = _deterministic_medication_status_tool_call(user_payload)
            if medication_status_tool_call:
                return {
                    "advice": "복약 담당 에이전트가 일정과 복용 상태를 확인할게요.",
                    "tool_calls": [
                        {
                            "name": DELEGATE_TO_MEDICATION_AGENT,
                            "arguments": {
                                "task": "check the requested medication schedule and dose status",
                                "reason": "patient asked for medication schedule or dose status",
                            },
                        }
                    ],
                }
            preference_tool_calls = _deterministic_preference_tool_calls(user_payload)
            if preference_tool_calls:
                return {
                    "advice": "말씀하신 식사 선호와 제한을 저장해둘게요.",
                    "tool_calls": preference_tool_calls,
                }
            policy_tool_call = _deterministic_policy_tool_call(user_payload)
            if policy_tool_call:
                return {
                    "advice": "알림 정책 변경 후보를 준비해볼게요.",
                    "tool_calls": [policy_tool_call],
                }
            return {"message": _multiturn_general_reply(user_payload)}
        return {"message": "요청을 확인했습니다."}


def _missed_slots(payload: dict[str, Any]) -> list[str]:
    if _observed_day_count(payload) < 2:
        return []
    slots: list[str] = []
    for summary in payload.get("slot_summaries") or []:
        if isinstance(summary, dict) and _summary_has_repeated_miss_pattern(summary) and summary.get("slot_label"):
            slots.append(str(summary["slot_label"]))
    return list(dict.fromkeys(slots))


def _observed_day_count(payload: dict[str, Any]) -> int:
    raw_count = payload.get("observed_day_count") or payload.get("window_days") or 1
    try:
        return int(raw_count)
    except (TypeError, ValueError):
        return 1


def _summary_has_repeated_miss_pattern(summary: dict[str, Any]) -> bool:
    try:
        scheduled_count = int(summary.get("scheduled_count") or 0)
        missed_count = int(summary.get("missed_count") or 0)
        miss_rate = float(summary.get("miss_rate") or 0.0)
    except (TypeError, ValueError):
        return False
    return missed_count >= 2 or (scheduled_count >= 3 and miss_rate >= 0.5)


def _notification_policy_tool_call(slot_label: str, payload: dict[str, Any]) -> dict[str, Any]:
    current_date = _payload_date(payload)
    return {
        "name": PROPOSE_NOTIFICATION_POLICY,
        "arguments": {
            "slot_label": slot_label,
            "extra_reminders": 2,
            "interval_minutes": 10,
            "effective_start_date": current_date.isoformat(),
            "effective_end_date": (current_date + timedelta(days=7)).isoformat(),
            "reason": f"{slot_label} 미복용 패턴을 바탕으로 알림 강화를 제안합니다.",
            "source": "pattern_analysis",
        },
    }


def _payload_date(payload: dict[str, Any]) -> date:
    raw_date = payload.get("date") or payload.get("current_time")
    if isinstance(raw_date, datetime):
        return raw_date.date()
    if isinstance(raw_date, date):
        return raw_date
    if isinstance(raw_date, str):
        return date.fromisoformat(raw_date[:10])
    return date.today()


def _deterministic_side_effect_tool_call(payload: dict[str, Any]) -> dict[str, Any] | None:
    message = str(payload.get("message") or "").strip()
    if not message:
        return None
    side_effect_keywords = ("메스꺼", "구역", "구토", "속이", "설사", "두통", "어지러", "부작용", "약 때문")
    if not any(keyword in message for keyword in side_effect_keywords):
        return None
    return {
        "name": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
        "arguments": {
            "symptom_text": message,
        },
    }


def _deterministic_medication_status_tool_call(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_medication_status_query(str(payload.get("message") or "")):
        return None
    return {
        "name": GET_MEDICATION_DOSE_STATUS,
        "arguments": {
            "target_date": _payload_date(payload).isoformat(),
        },
    }


def _is_medication_status_query(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    mutation_phrases = ("기록해", "등록해", "바꿔", "변경해", "완료로", "취소해", "삭제해")
    if any(phrase in text for phrase in mutation_phrases):
        return False
    medication_terms = ("약", "복약", "복용")
    query_terms = (
        "언제",
        "몇 시",
        "몇시",
        "남은 약",
        "남았",
        "복용 상태",
        "복약 상태",
        "복약 일정",
        "복용 일정",
        "먹을 약",
        "복용할 약",
        "먹어야",
        "복용했는지",
        "먹었는지",
        "약 있어",
        "약이 있어",
        "약은 뭐",
        "약이 뭐",
        "약 몇 개",
    )
    return any(term in text for term in medication_terms) and any(term in text for term in query_terms)


def _deterministic_policy_tool_call(payload: dict[str, Any]) -> dict[str, Any] | None:
    message = str(payload.get("message") or "").strip()
    if "알림" not in message or not any(keyword in message for keyword in ("바꿔", "변경", "늘려", "줄여", "간격")):
        return None
    current_date = _payload_date(payload)
    slot_label = "아침 08:00" if "아침" in message else "야간 21:00" if "야간" in message or "저녁" in message else "아침 08:00"
    extra_reminders = 2 if "2" in message or "두" in message else 1
    interval_minutes = 10 if "10" in message else 30
    return {
        "name": PROPOSE_NOTIFICATION_POLICY,
        "arguments": {
            "slot_label": slot_label,
            "extra_reminders": extra_reminders,
            "interval_minutes": interval_minutes,
            "effective_start_date": current_date.isoformat(),
            "effective_end_date": (current_date + timedelta(days=7)).isoformat(),
            "reason": "사용자가 채팅에서 알림 정책 변경을 요청했습니다.",
            "source": "patient_request",
        },
    }


def _deterministic_preference_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    message = str(payload.get("message") or "").strip()
    if not message:
        return []
    candidates = {
        "짜장면": "food",
        "탕수육": "food",
        "콤비네이션피자": "food",
        "피자": "food",
        "콜라": "food",
        "땅콩": "ingredient",
        "한식": "cuisine",
    }
    calls: list[dict[str, Any]] = []
    for label, object_type in candidates.items():
        index = message.find(label)
        if index < 0:
            continue
        window = _preference_window(message, label, candidates)
        predicate = ""
        if any(keyword in window for keyword in ("알레르기", "알러지")):
            predicate = "allergic_to"
        elif any(keyword in window for keyword in ("의사가 피하", "먹지 말", "금지", "제한")):
            predicate = "medically_avoids"
        elif any(keyword in window for keyword in ("싫", "안 좋아", "별로")):
            predicate = "dislikes"
        elif any(keyword in window for keyword in ("좋아", "선호", "위주")):
            predicate = "prefers" if object_type in {"cuisine", "diet_style"} else "likes"
        if not predicate:
            continue
        calls.append(
            {
                "name": UPSERT_NUTRITION_PREFERENCE_FACT,
                "arguments": {
                    "predicate": predicate,
                    "object_label": label,
                    "object_type": object_type,
                    "confidence": 0.9,
                    "strength": 1.0,
                    "evidence_text": message,
                },
            }
        )
    return calls


def _preference_window(message: str, label: str, candidates: dict[str, str]) -> str:
    index = message.find(label)
    if index < 0:
        return ""
    next_indices = [
        found
        for other_label in candidates
        if other_label != label
        for found in [message.find(other_label, index + len(label))]
        if found >= 0
    ]
    boundary = min(next_indices) if next_indices else min(len(message), index + 28)
    return message[index:boundary]


def _multiturn_general_reply(payload: dict[str, Any]) -> str:
    message = str(payload.get("message") or "").strip()
    recent_chat = _deterministic_context_value(payload, "recent_chat")
    if not isinstance(recent_chat, list):
        recent_chat = []
    previous_turns = [
        item
        for item in recent_chat
        if isinstance(item, dict) and str(item.get("content") or "").strip() and str(item.get("content") or "").strip() != message
    ]
    if any(keyword in message for keyword in ("아까", "방금", "이전", "전에", "뭔말", "뭐라", "무슨 말", "기억")) and previous_turns:
        snippets = [str(item.get("content") or "").strip() for item in previous_turns[-4:]]
        return "앞선 대화에서는 " + " / ".join(snippets) + " 라고 이야기했습니다."
    if _is_nutrition_medication_message(message):
        return (
            "영양과 복약을 함께 보면, 오늘 짠 식사를 하셨다면 저녁은 국물과 가공식품을 줄이고 채소와 단백질을 먼저 챙기세요. "
            "당 수치가 걱정된다면 탄수화물 양을 평소보다 조금 낮추고, 처방된 복약 시간은 임의로 바꾸지 말고 예정된 시간에 맞춰 복용하세요."
        )
    if _is_nutrition_message(message):
        return "오늘 식사는 나트륨과 탄수화물을 조금 낮추고 채소, 단백질, 수분을 함께 보강하는 방향이 좋겠습니다."
    if previous_turns:
        return "앞선 대화는 확인했습니다. 복약 일정·복용 여부, 부작용 증상, 식사 기록, 알림 변경 중 어떤 내용을 이어서 도와드릴까요?"
    return "복약 일정·복용 여부 조회, 부작용 증상 확인, 식사 기록, 알림 변경 중 필요한 내용을 조금 더 구체적으로 알려주세요."


def _deterministic_context_value(payload: dict[str, Any], key: str) -> Any:
    direct_value = context_value(payload, key)
    if direct_value is not None:
        return direct_value
    backend_context = context_value(payload, "backend_read_context")
    if isinstance(backend_context, dict):
        return backend_context.get(key)
    return None


class DeterministicTestChatModel(BaseChatModel):
    """LangChain adapter for deterministic contract and browser tests."""

    provider: DeterministicTestProvider
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "deterministic_test_chat_model"

    def bind_tools(self, tools, *, tool_choice: str | None = None, **kwargs):
        return self.model_copy(update={"bound_tools": list(tools)})

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        return asyncio.run(self._agenerate(messages, stop=stop, run_manager=None, **kwargs))

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        delay_ms = max(
            0,
            int(os.getenv("DETERMINISTIC_TEST_PROVIDER_DELAY_MS", "0")),
        )
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        tool_results = tool_results_from_messages(messages)
        if tool_results:
            fallback = _deterministic_tool_result_summary(tool_results)
            return _chat_result(AIMessage(content=fallback, response_metadata={"model_output": {"message": fallback, "fallback": "tool_result_summary"}}))

        payload = human_payload_from_messages(messages)
        output = self.provider.model_output(payload)
        if not isinstance(output, dict):
            output = {}
        validate_llm_output(str(payload.get("decision_type") or "system_guidance"), output, payload)
        tool_calls = normalize_tool_calls(output)
        if tool_calls:
            return _chat_result(ai_message_from_tool_calls(tool_calls, content=natural_chat_summary(output), model_output=output))
        return _chat_result(AIMessage(content=natural_chat_summary(output), response_metadata={"model_output": output}))


def _chat_result(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


def _deterministic_tool_result_summary(results: list[Any]) -> str:
    if results:
        last = results[-1]
        if last.tool_name in DELEGATION_TOOL_NAMES and last.status in {"success", "confirmation_required"}:
            delegated_summary = str(last.response.get("human_summary") or "").strip()
            if delegated_summary:
                return delegated_summary
    return tool_result_summary(results, "도구 실행 결과를 확인했습니다.")


def _is_nutrition_medication_message(message: str) -> bool:
    return _is_nutrition_message(message) and any(keyword in message for keyword in ("복약", "복용", "약", "처방"))


def _is_nutrition_message(message: str) -> bool:
    nutrition_keywords = ("식사", "아침", "점심", "저녁", "영양", "나트륨", "짜", "당 수치", "혈당", "탄수화물", "열량", "칼로리")
    return any(keyword in message for keyword in nutrition_keywords)
