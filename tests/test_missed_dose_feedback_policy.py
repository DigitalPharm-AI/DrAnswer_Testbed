from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.schemas import AgentResponse
from system_app.services.adherence_pattern_service import (
    AdherencePatternDecision,
    StreakMetrics,
)
from system_app.services.missed_dose_agent_response_service import (
    missed_dose_adherence_pattern_message,
)
from system_app.services.tone_policy_service import TonePolicyDecision


class FakeSession:
    def __init__(self, event) -> None:
        self.event = event

    def get(self, _model, _identifier):
        return self.event


def _pattern_decision() -> AdherencePatternDecision:
    return AdherencePatternDecision(
        pattern_code="B",
        pattern_label="습관 미형성",
        tone="루틴 형성 지원",
        message="이 문구는 정책 카탈로그 값이며 전달하면 안 됩니다.",
        reason="복약 루틴 형성을 위한 단기 미복용 확인이 필요합니다.",
        streak_metrics=StreakMetrics(
            current_consecutive_missed_days=1,
            previous_consecutive_taken_days=0,
            slot_scheduled_count=1,
            slot_taken_count=0,
            slot_missed_count=1,
            overall_scheduled_count=1,
            overall_taken_count=0,
            overall_missed_count=1,
            overall_adherence_rate=0.0,
            prescription_day_count=1,
            recent_side_effect_keep=False,
        ),
    )


def _tone_decision() -> TonePolicyDecision:
    return TonePolicyDecision(
        pattern_code="B",
        tone_key="persuasion",
        policy_variant="missed_dose.persuasion.v1",
        intensity=1,
        message="이 문구도 정책 카탈로그 값이며 전달하면 안 됩니다.",
        message_variant="v1",
        message_catalog_source="test_catalog",
        selection_reason="default_for_pattern_b",
        slot_label="아침",
    )


def _response(message: str) -> AgentResponse:
    return AgentResponse(
        trace_id="trace-feedback-policy",
        agent_name="missed_dose_coach",
        prompt_version_id="missed_dose_generation_v1",
        decision_type="missed_dose_assessment",
        structured_payload={},
        human_summary=message,
        requires_conversation_alert=True,
    )


def test_backend_uses_verified_llm_message_and_keeps_policy_authority(
    monkeypatch,
) -> None:
    from system_app.services import missed_dose_agent_response_service as service

    monkeypatch.setattr(
        service,
        "evaluate_adherence_pattern",
        lambda _session, _event: _pattern_decision(),
    )
    monkeypatch.setattr(
        service,
        "select_tone_policy",
        lambda _session, _event, _pattern_code: _tone_decision(),
    )
    message = "복약을 놓친 상황을 확인하고 싶어요. 어떤 어려움이 있었나요?"
    metadata: dict = {}

    delivered = missed_dose_adherence_pattern_message(
        FakeSession(SimpleNamespace(medication_name="예시약")),
        related_dose_event_id=1,
        message_metadata=metadata,
        response=_response(message),
    )

    assert delivered == message
    assert metadata["adherence_pattern"]["pattern_code"] == "B"
    assert metadata["adherence_pattern"]["message"] == message
    assert metadata["tone_policy"]["tone_key"] == "persuasion"
    assert metadata["tone_policy"]["delivered_message_source"] == "llm_generated"
    assert "message" not in metadata["tone_policy"]
    assert metadata["llm_personalization"]["adjudication_source"] == "backend_policy"
    assert metadata["llm_personalization"]["message_source"] == "llm_generated"


def test_backend_rejects_unsafe_llm_message_without_catalog_fallback(
    monkeypatch,
) -> None:
    from system_app.services import missed_dose_agent_response_service as service

    monkeypatch.setattr(
        service,
        "evaluate_adherence_pattern",
        lambda _session, _event: _pattern_decision(),
    )
    monkeypatch.setattr(
        service,
        "select_tone_policy",
        lambda _session, _event, _pattern_code: _tone_decision(),
    )

    with pytest.raises(
        ValueError,
        match="missed_dose_llm_message_invalid",
    ):
        missed_dose_adherence_pattern_message(
            FakeSession(SimpleNamespace(medication_name="예시약")),
            related_dose_event_id=1,
            message_metadata={},
            response=_response("예시약은 반드시 복용하세요."),
        )
