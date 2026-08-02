from __future__ import annotations

from datetime import UTC, datetime

from agent_app.tools.medication_dose_resolution import (
    resolve_dose_target,
)

NOW = datetime(2026, 4, 20, 9, 30, tzinfo=UTC)


def _payload(events: list[dict]) -> dict:
    return {
        "patient_id": "patient_0000000000000801",
        "message": "메트포르핀 복용했어 아침에",
        "current_time": NOW,
        "context": {
            "trusted_patient_context": {
                "today_medication": {
                    "dose_events": events,
                }
            }
        },
    }


def _event(
    dose_event_id: str,
    medication_name: str,
    *,
    hour: int,
    slot_label: str = "아침",
    status: str = "scheduled",
) -> dict:
    return {
        "dose_event_id": dose_event_id,
        "medication_name": medication_name,
        "slot_label": slot_label,
        "scheduled_for": NOW.replace(hour=hour).isoformat(),
        "status": status,
    }


def test_typo_resolves_to_one_trusted_dose_event() -> None:
    events = [
        _event("dose_metformin", "메트포르민 500mg", hour=8),
        _event("dose_amlodipine", "암로디핀 5mg", hour=9),
    ]

    result = resolve_dose_target(
        {"medication_name": "메트포르핀"},
        payload=_payload(events),
    )

    assert result["kind"] == "resolved"
    assert result["dose_event_id"] == "dose_metformin"


def test_same_medication_in_two_slots_requires_selection() -> None:
    events = [
        _event("dose_morning", "메트포르민 500mg", hour=8),
        _event(
            "dose_evening",
            "메트포르민 500mg",
            hour=18,
            slot_label="저녁",
        ),
    ]
    payload = _payload(events)
    payload["message"] = "메트포르민 먹었어"

    result = resolve_dose_target(
        {"medication_name": "메트포르민"},
        payload=payload,
    )

    assert result["kind"] == "selection_required"
    assert {
        item["dose_event_id"]
        for item in result["candidates"]
    } == {"dose_morning", "dose_evening"}
    assert len(result["selection_request"]["selections"]) == 2


def test_no_trusted_match_requires_clarification() -> None:
    result = resolve_dose_target(
        {"medication_name": "없는 약"},
        payload=_payload(
            [_event("dose_metformin", "메트포르민 500mg", hour=8)]
        ),
    )

    assert result["kind"] == "clarification_required"


def test_already_taken_is_a_noop() -> None:
    result = resolve_dose_target(
        {"medication_name": "메트포르민"},
        payload=_payload(
            [
                _event(
                    "dose_metformin",
                    "메트포르민 500mg",
                    hour=8,
                    status="taken",
                )
            ]
        ),
    )

    assert result["kind"] == "already_taken"
    assert result["dose_event_id"] == "dose_metformin"


def test_untrusted_supplied_identifier_is_not_accepted() -> None:
    result = resolve_dose_target(
        {"dose_event_id": "dose_invented"},
        payload=_payload(
            [_event("dose_metformin", "메트포르민 500mg", hour=8)]
        ),
    )

    assert result["kind"] == "clarification_required"
