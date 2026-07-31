from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agent_app.jobs import daily_pattern_tasks as pattern_tasks
from agent_app.jobs import worker as agent_worker
from shared.schemas import AgentResponse


def test_v13_backend_trigger_uses_same_day_0830_kst_delivery() -> None:
    received_at = datetime(2026, 7, 28, 17, 0, tzinfo=UTC)

    assert pattern_tasks.v13_proposal_delivery_run_after(
        received_at=received_at,
    ) == datetime(2026, 7, 28, 23, 30)


def _response(tool_calls: list[dict]) -> AgentResponse:
    return AgentResponse(
        trace_id="trace-pattern",
        agent_name="daily_pattern_agent",
        prompt_version_id="test",
        decision_type="tool_call" if tool_calls else "pattern_analysis",
        structured_payload={"tool_calls": tool_calls},
        human_summary="분석을 완료했습니다.",
    )


def _proposal_call(*, extra_reminders: int, reason: str) -> dict:
    return {
        "name": "propose_notification_policy",
        "arguments": {
            "policy_key": "slot-policy",
            "slot_label": "아침",
            "extra_reminders": extra_reminders,
            "interval_minutes": 30,
            "missed_dose_after_minutes": 90,
            "primary_reminder_timing": "at",
            "primary_reminder_offset_minutes": 0,
            "effective_start_date": "2026-07-29",
            "effective_end_date": "2026-08-04",
            "reason": reason,
            "source": "pattern_analysis",
        },
    }


def test_no_proposal_produces_no_callback_candidate() -> None:
    assert pattern_tasks._single_proposal(_response([])) is None


def test_one_proposal_maps_to_minimal_external_policy_fields() -> None:
    proposal = pattern_tasks._single_proposal(
        _response(
            [
                _proposal_call(
                    extra_reminders=2,
                    reason="최근 아침 미복용 빈도가 증가했습니다.",
                )
            ]
        )
    )

    assert proposal is not None
    assert proposal["reason"] == "최근 아침 미복용 빈도가 증가했습니다."
    assert proposal["proposed_policy"].model_dump(
        mode="json",
        exclude_none=True,
    ) == {
        "extra_reminders": 2,
        "interval_minutes": 30,
        "missed_dose_after_minutes": 90,
        "primary_reminder_timing": "at",
        "primary_reminder_offset_minutes": 0,
        "effective_start_date": "2026-07-29",
        "effective_end_date": "2026-08-04",
    }


def test_identical_duplicate_proposals_collapse_to_one() -> None:
    call = _proposal_call(
        extra_reminders=2,
        reason="같은 제안",
    )
    proposal = pattern_tasks._single_proposal(_response([call, call]))
    assert proposal is not None


def test_distinct_multiple_proposals_fail_without_priority_contract() -> None:
    with pytest.raises(
        pattern_tasks.DailyPatternProposalPriorityUndefined,
        match="daily_pattern_multiple_proposals_require_priority_contract",
    ):
        pattern_tasks._single_proposal(
            _response(
                [
                    _proposal_call(
                        extra_reminders=1,
                        reason="첫 번째 제안",
                    ),
                    _proposal_call(
                        extra_reminders=2,
                        reason="두 번째 제안",
                    ),
                ]
            )
        )


def test_delivery_task_posts_only_minimal_bearer_callback(
    monkeypatch,
) -> None:
    callback_calls: list[dict] = []

    def fake_persist(
        task_id,
        callback_payload,
        *,
        callback_path,
        callback_bearer,
    ):
        assert task_id == 77
        assert callback_bearer is True
        assert callback_path == (
            "/api/agent/async/"
            "notification-policy-change-proposals"
        )
        assert set(callback_payload) == {
            "request_id",
            "patient_id",
            "proposed_policy",
            "reason",
        }
        return callback_payload

    async def fake_post(context, path, payload, *, bearer=False):
        callback_calls.append(
            {
                "context": context,
                "path": path,
                "payload": payload,
                "bearer": bearer,
            }
        )

    monkeypatch.setattr(
        agent_worker,
        "_persist_callback_payload",
        fake_persist,
    )
    monkeypatch.setattr(agent_worker, "_post_callback", fake_post)
    asyncio.run(
        agent_worker._execute_snapshot(
            {
                "id": 77,
                "request_id": "req_00000000dad30001",
                "task_type": pattern_tasks.DAILY_PATTERN_DELIVERY_TASK,
                "payload": {
                    "request_id": "req_00000000dad30001",
                    "patient_id": "patient_00000000dad30001",
                    "proposed_policy": {
                        "extra_reminders": 2,
                    },
                    "reason": "최근 미복용 빈도가 증가했습니다.",
                },
                "callback_context": {
                    "app_base_url": "http://backend.test",
                },
            },
            object(),
        )
    )

    assert len(callback_calls) == 1
    assert callback_calls[0]["path"].endswith(
        "/notification-policy-change-proposals"
    )
    assert callback_calls[0]["bearer"] is True
