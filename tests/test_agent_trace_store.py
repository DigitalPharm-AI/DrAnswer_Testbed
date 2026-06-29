from __future__ import annotations

import json

from sqlalchemy import select

from shared.schemas import AgentResponse
from shared.settings import get_settings
from system_app.models import AgentRunStep, AgentRunTrace
from system_app.services.agent_trace_store import agent_trace_payload, record_agent_run_failure, upsert_agent_run_trace
from tests.helpers import build_session


def test_agent_trace_store_persists_redacted_costed_steps(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "rule_based")
    monkeypatch.setenv("LLM_MODEL_TIER", "fast")
    monkeypatch.setenv("AGENT_COST_INPUT_USD_PER_1M_TOKENS", "2")
    monkeypatch.setenv("AGENT_COST_OUTPUT_USD_PER_1M_TOKENS", "10")
    get_settings.cache_clear()
    try:
        response = AgentResponse(
            trace_id="trace-p1-observability",
            agent_name="nutrition_medication_agent",
            prompt_version_id="prompt-v1",
            decision_type="nutrition_meal_recorded",
            structured_payload={
                "elapsed_ms": 123,
                "token_usage": {"input_tokens": 1000, "output_tokens": 250},
                "tool_calls": [
                    {
                        "name": "record_meal",
                        "arguments": {
                            "patient_id": "patient-a",
                            "foods": [{"food_name": "private noodle", "portion": "1 bowl"}],
                        },
                    }
                ],
                "tool_results": [{"tool_name": "record_meal", "status": "success", "response": {"meal_id": 1}}],
            },
            human_summary="private meal summary",
        )

        with build_session() as session:
            trace = upsert_agent_run_trace(
                session,
                response,
                workflow_name="multiturn_chat",
                source_event_type="agent_async_chat_result",
                request_id="chat:req:1",
                patient_id="patient-a",
                request_message="private symptom meal note",
            )
            session.commit()

            stored_trace = session.scalar(select(AgentRunTrace).where(AgentRunTrace.trace_id == trace.trace_id))
            steps = list(session.scalars(select(AgentRunStep).where(AgentRunStep.trace_id == trace.trace_id)))
            payload = agent_trace_payload(stored_trace, steps)

        assert stored_trace is not None
        assert stored_trace.patient_id_hash
        assert stored_trace.patient_id_hash != "patient-a"
        assert stored_trace.input_hash
        assert stored_trace.output_hash
        assert stored_trace.input_tokens == 1000
        assert stored_trace.output_tokens == 250
        assert stored_trace.estimated_cost_usd == 0.0045
        assert stored_trace.latency_ms == 123
        assert stored_trace.tool_count == 1
        assert len(steps) == 3
        assert [step.step_type for step in steps] == ["model_call", "tool_call", "final_response"]
        assert steps[1].tool_name == "record_meal"
        assert steps[1].side_effect_level == "write"

        raw_trace_text = json.dumps(payload, ensure_ascii=False)
        assert "patient-a" not in raw_trace_text
        assert "private noodle" not in raw_trace_text
        assert "private symptom meal note" not in raw_trace_text
        assert "private meal summary" not in raw_trace_text
    finally:
        get_settings.cache_clear()


def test_agent_trace_store_replaces_steps_on_replay():
    with build_session() as session:
        first = AgentResponse(
            trace_id="trace-p1-replay",
            agent_name="agent",
            prompt_version_id="v1",
            decision_type="first",
            structured_payload={"tool_calls": [{"name": "list_meals"}]},
            human_summary="first summary",
        )
        second = AgentResponse(
            trace_id="trace-p1-replay",
            agent_name="agent",
            prompt_version_id="v1",
            decision_type="second",
            structured_payload={"tool_calls": [{"name": "record_meal"}]},
            human_summary="second summary",
        )

        upsert_agent_run_trace(session, first, workflow_name="chat", source_event_type="callback")
        upsert_agent_run_trace(session, second, workflow_name="chat", source_event_type="callback")
        session.commit()

        trace = session.scalar(select(AgentRunTrace).where(AgentRunTrace.trace_id == "trace-p1-replay"))
        steps = list(session.scalars(select(AgentRunStep).where(AgentRunStep.trace_id == "trace-p1-replay")))

    assert trace is not None
    assert trace.decision_type == "second"
    assert trace.tool_count == 1
    assert len([step for step in steps if step.step_type == "tool_call"]) == 1
    assert next(step for step in steps if step.step_type == "tool_call").tool_name == "record_meal"


def test_agent_trace_store_records_failure_without_agent_response():
    with build_session() as session:
        trace = record_agent_run_failure(
            session,
            trace_id="trace-p1-failure",
            workflow_name="missed_dose",
            source_event_type="agent_async_failure",
            request_id="missed_dose:job:1",
            patient_id="patient-a",
            request_message="private symptom note",
            agent_name="missed_dose_agent",
            decision_type="provider_failure",
            error_message="provider failed token=secret-value",
            job_id=1,
        )
        session.commit()

        steps = list(session.scalars(select(AgentRunStep).where(AgentRunStep.trace_id == "trace-p1-failure")))
        payload = agent_trace_payload(trace, steps)

    assert trace is not None
    assert trace.status == "failed"
    assert trace.agent_name == "missed_dose_agent"
    assert trace.decision_type == "provider_failure"
    assert trace.patient_id_hash != "patient-a"
    assert len(steps) == 2
    assert [step.status for step in steps] == ["failed", "failed"]
    raw_trace_text = json.dumps(payload, ensure_ascii=False)
    assert "patient-a" not in raw_trace_text
    assert "private symptom note" not in raw_trace_text
    assert "secret-value" not in raw_trace_text
