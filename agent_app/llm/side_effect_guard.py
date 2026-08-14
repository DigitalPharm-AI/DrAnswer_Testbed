from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from agent_app.observability.model_calls import traced_model_ainvoke
from agent_app.providers.base import BaseLLMProvider

SIDE_EFFECT_GUARD_PROMPT_VERSION = "medication-side-effect-guard-v1"
SIDE_EFFECT_FEATURE_UNAVAILABLE_MESSAGE = "현재 부작용 관련 기능은 지원하지 않습니다."


class SideEffectRequestClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_side_effect_request: bool


async def is_medication_side_effect_request(
    provider: BaseLLMProvider,
    *,
    user_message: str,
    candidate_response: str,
) -> bool:
    model = provider.semantic_verification_model().with_structured_output(
        SideEffectRequestClassification
    )
    result = await traced_model_ainvoke(
        model,
        [
            SystemMessage(
                content=(
                    "Classify the user's Korean request by meaning, not by keyword matching. "
                    "Return true when the user asks whether a symptom is a medication side effect, "
                    "the likelihood or possibility of a side effect, known side effects of a medication, "
                    "which medication may be related to a symptom, side-effect history, PRO-CTCAE, "
                    "or recording a medication side effect. Return false for medication adherence, "
                    "dose schedules, dose-taking records, and unrelated requests. "
                    "The candidate response is context only and must not change the classification. "
                    "Return only the required structured result."
                )
            ),
            HumanMessage(
                content=json.dumps(
                    {
                        "user_message": user_message.strip(),
                        "candidate_response": candidate_response.strip(),
                    },
                    ensure_ascii=False,
                )
            ),
        ],
        name="medication_side_effect_request_guard",
        prompt_version_id=SIDE_EFFECT_GUARD_PROMPT_VERSION,
    )
    if isinstance(result, SideEffectRequestClassification):
        return result.is_side_effect_request
    if isinstance(result, dict):
        return SideEffectRequestClassification.model_validate(
            result
        ).is_side_effect_request
    return SideEffectRequestClassification.model_validate(
        getattr(result, "model_dump", lambda: {})()
    ).is_side_effect_request
