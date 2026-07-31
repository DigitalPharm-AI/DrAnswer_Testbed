from datetime import date

from shared.schemas import NotificationPolicyDelta
from system_app.services.policy_service import validate_policy_delta


def test_policy_validation_accepts_valid_policy():
    delta = NotificationPolicyDelta(
        slot_label="아침 08:00",
        extra_reminders=2,
        interval_minutes=5,
        effective_start_date=date(2026, 4, 20),
        effective_end_date=date(2026, 4, 27),
        reason="아침 누락 빈도 증가",
        source="pattern_analysis",
    )

    is_valid, _ = validate_policy_delta(delta)

    assert is_valid is True


def test_policy_validation_accepts_one_minute_interval():
    delta = NotificationPolicyDelta(
        slot_label="아침 08:00",
        extra_reminders=1,
        interval_minutes=1,
        effective_start_date=date(2026, 4, 20),
        effective_end_date=date(2026, 4, 27),
        reason="테스트",
        source="patient_request",
    )

    is_valid, message = validate_policy_delta(delta)

    assert is_valid is True
    assert message == "ok"
