from datetime import datetime

from shared.json_utils import parse_json_object
from system_app.services.missed_dose_reply_understanding import (
    AGENT_RESPONSE_COMPLETED,
    PENDING_AGENT_INTERPRETATION,
    complete_missed_dose_reply,
    missed_dose_reply_request_metadata,
)
from system_app.services.notification_service import create_notification
from tests.helpers import build_session


def test_missed_dose_reply_context_is_neutral_until_agent_completes():
    with build_session() as session:
        alert = create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="미복용 이유를 알려주세요.",
            visible_at=datetime(2026, 4, 20, 9, 30),
            metadata={"category": "missed_dose", "status": "agent_ready"},
        )

        metadata = missed_dose_reply_request_metadata(alert)

        assert metadata == {
            "missed_dose_reply": {
                "conversation_alert_id": alert.id,
                "related_dose_event_id": None,
                "status": PENDING_AGENT_INTERPRETATION,
            }
        }
        assert "understanding" not in metadata["missed_dose_reply"]
        assert "fallback_understanding" not in metadata["missed_dose_reply"]
        assert alert.acknowledged is False


def test_missed_dose_alert_closes_only_after_agent_response_persists():
    with build_session() as session:
        alert = create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="미복용 이유를 알려주세요.",
            visible_at=datetime(2026, 4, 20, 9, 30),
            metadata={"category": "missed_dose", "status": "agent_ready"},
        )

        complete_missed_dose_reply(
            session,
            notification_id=alert.id,
            assistant_message_id="assistant_msg_completed",
        )

        stored = session.get(type(alert), alert.id)
        assert stored is not None
        metadata = parse_json_object(stored.metadata_json)
        assert metadata["status"] == AGENT_RESPONSE_COMPLETED
        assert metadata["assistant_message_id"] == "assistant_msg_completed"
        assert "missed_dose_reply_understanding" not in metadata
        assert stored.acknowledged is True
