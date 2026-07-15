from datetime import datetime

from shared.json_utils import parse_json_object
from shared.schemas import AgentResponse
from system_app.models import Notification
from system_app.services.missed_dose_reply_understanding import (
    UNDERSTANDING_METADATA_KEY,
    build_rule_based_missed_dose_reply_understanding,
    merge_missed_dose_reply_understanding_from_agent_response,
    missed_dose_reply_request_metadata,
)
from system_app.services.notification_service import create_notification
from system_app.services.system_request_service import create_system_event_request
from tests.helpers import build_session


def test_rule_based_understanding_detects_side_effect_signal():
    result = build_rule_based_missed_dose_reply_understanding("속이 메스꺼워서 못 먹었어")

    assert result["reply_intent"] == "missed_reason"
    assert result["barrier_type"] == "side_effect_concern"
    assert result["reaction_action"] == "reply"
    assert result["policy_signals"]["prefer_tone"] == "side_effect_check"
    assert result["policy_signals"]["needs_side_effect_check"] is True
    assert "warning_soft" in result["policy_signals"]["avoid_tones"]


def test_rule_based_understanding_detects_forgetfulness_and_delay():
    forgetful = build_rule_based_missed_dose_reply_understanding("깜빡했어요")
    delayed = build_rule_based_missed_dose_reply_understanding("이따가 먹을게요")

    assert forgetful["barrier_type"] == "forgetfulness"
    assert forgetful["policy_signals"]["prefer_tone"] == "practical"
    assert delayed["reply_intent"] == "snooze_request"
    assert delayed["reaction_action"] == "snooze"


def test_agent_understanding_merge_updates_request_alert_and_user_message_metadata():
    with build_session() as session:
        alert = create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="미복용 이유를 알려주세요.",
            visible_at=datetime(2026, 4, 20, 9, 30),
            metadata={"category": "missed_dose", "status": "reply_submitted", "patient_reply": "회의 때문에 바빴어요"},
        )
        initial = build_rule_based_missed_dose_reply_understanding("회의 때문에 바빴어요")
        request = create_system_event_request(
            session,
            "multiturn_chat",
            "회의 때문에 바빴어요",
            datetime(2026, 4, 20, 9, 31),
            metadata=missed_dose_reply_request_metadata(alert, initial),
        )
        response = AgentResponse(
            trace_id="trace-understanding",
            agent_name="multiturn_chat_agent",
            prompt_version_id="v",
            decision_type="system_guidance",
            structured_payload={
                "model_output": {
                    UNDERSTANDING_METADATA_KEY: {
                        "reply_intent": "missed_reason",
                        "barrier_type": "busy",
                        "reaction_action": "reply",
                        "confidence": 0.91,
                        "evidence": ["agent_busy_context"],
                        "policy_signals": {"prefer_tone": "practical"},
                    }
                }
            },
            human_summary="바쁜 상황이었군요.",
            requires_conversation_alert=False,
        )

        merged = merge_missed_dose_reply_understanding_from_agent_response(session, request.id, response)

        assert merged is not None
        assert merged["source"] == "agent_structured_payload"
        assert merged["barrier_type"] == "busy"
        request_metadata = parse_json_object(session.get(Notification, request.id).metadata_json)
        alert_metadata = parse_json_object(session.get(Notification, alert.id).metadata_json)
        assert request_metadata["missed_dose_reply"]["understanding"]["barrier_type"] == "busy"
        assert alert_metadata[UNDERSTANDING_METADATA_KEY]["barrier_type"] == "busy"
