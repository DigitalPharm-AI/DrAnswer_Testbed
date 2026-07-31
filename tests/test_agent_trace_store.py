from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import agent_app.persistence.trace_store as trace_store_module
from agent_app.llm.messages import model_output_from_ai_message
from agent_app.observability.evidence import TraceEvidenceContext
from agent_app.persistence.models import (
    AgentRunStep,
    AgentRunTrace,
    AgentToolExecution,
)
from agent_app.observability.model_calls import (
    capture_model_calls,
    response_with_model_calls,
    traced_model_ainvoke,
)
from agent_app.persistence.trace_store import AgentTraceStore
from shared.chat_contracts import ChatSyncRequest
from shared.retention_policy import (
    AGENT_OBSERVABILITY_RETENTION_DAYS,
)
from shared.schemas import AgentResponse
from tests.helpers import build_agent_engine


def _settings():
    return SimpleNamespace(
        app_env="test",
        app_release_version="test-release",
        llm_provider="bedrock_anthropic",
        llm_model_tier="sonnet",
        agent_cost_input_usd_per_1m_tokens=2.0,
        agent_cost_output_usd_per_1m_tokens=4.0,
        model_id_for_tier=lambda _tier=None: "test-model",
        require_agent_feedback_encryption=lambda: (
            "test-feedback-v1",
            b"t" * 32,
        ),
    )


def _store():
    engine, _cleanup = build_agent_engine("agent_trace_store")
    factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        future=True,
    )
    return AgentTraceStore(factory, settings=_settings()), factory


def test_agent_trace_store_is_redacted_costed_and_fixed_to_three_years(
    monkeypatch,
):
    fixed_now = datetime(2026, 7, 28, 9, 0)
    monkeypatch.setattr(
        trace_store_module,
        "utc_now",
        lambda: fixed_now,
    )
    store, factory = _store()
    raw_patient_id = "patient-private"
    raw_symptom = "private nausea narrative"
    trace_id = "async-trace-retention"
    store.start_async_task(
        request_id="request-retention",
        task_type="missed_dose",
        payload={
            "patient_id": raw_patient_id,
            "symptom_text": raw_symptom,
        },
        trace_id=trace_id,
    )
    store.complete_async_task(
        request_id="request-retention",
        patient_id=raw_patient_id,
        response=AgentResponse(
            trace_id=trace_id,
            agent_name="missed_dose_coach",
            prompt_version_id="prompt-v1",
            decision_type="side_effect_assessment",
            structured_payload={
                "token_usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                },
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": (
                            "get_medication_side_effect_assessment"
                        ),
                        "arguments": {
                            "symptom_text": raw_symptom,
                        },
                    }
                ],
                "tool_results": [
                    {
                        "status": "success",
                        "response": {"evidence": raw_symptom},
                    }
                ],
            },
            human_summary="private assistant answer",
        ),
    )

    expected_expiry = fixed_now + timedelta(
        days=AGENT_OBSERVABILITY_RETENTION_DAYS
    )
    with factory() as session:
        trace = session.scalar(
            select(AgentRunTrace).where(
                AgentRunTrace.trace_id == trace_id
            )
        )
        assert trace is not None
        assert trace.status == "COMPLETED"
        assert trace.input_tokens == 1000
        assert trace.output_tokens == 500
        assert trace.estimated_cost_usd == 0.004
        assert trace.expires_at == expected_expiry
        assert trace.patient_id_hash != raw_patient_id
        assert len(trace.patient_id_hash) == 64
        assert raw_patient_id not in trace.metadata_json
        assert raw_symptom not in trace.metadata_json

        steps = list(
            session.scalars(
                select(AgentRunStep).where(
                    AgentRunStep.trace_id == trace_id
                )
            )
        )
        executions = list(
            session.scalars(
                select(AgentToolExecution).where(
                    AgentToolExecution.trace_id == trace_id
                )
            )
        )
        assert len(steps) == 3
        assert len(executions) == 1
        assert all(row.expires_at == expected_expiry for row in steps)
        assert executions[0].expires_at == expected_expiry
        assert raw_symptom not in executions[0].metadata_json


def test_failed_trace_keeps_redacted_failure_for_three_years(monkeypatch):
    fixed_now = datetime(2026, 7, 28, 10, 0)
    monkeypatch.setattr(
        trace_store_module,
        "utc_now",
        lambda: fixed_now,
    )
    store, factory = _store()
    trace_id = "async-trace-failed"
    store.start_async_task(
        request_id="request-failed",
        task_type="daily_pattern",
        payload={"patient_id": "patient-private"},
        trace_id=trace_id,
    )
    store.fail_chat(
        trace_id=trace_id,
        error_code="PROVIDER_ERROR",
        error_message=(
            "patient detail user@example.com token=private-secret"
        ),
        retryable=False,
    )

    with factory() as session:
        trace = session.scalar(
            select(AgentRunTrace).where(
                AgentRunTrace.trace_id == trace_id
            )
        )
        assert trace is not None
        assert trace.status == "FINAL_FAILED"
        assert "user@example.com" not in trace.error_message
        assert "private-secret" not in trace.error_message
        assert trace.expires_at == fixed_now + timedelta(
            days=AGENT_OBSERVABILITY_RETENTION_DAYS
        )


def test_langchain_usage_metadata_is_carried_into_model_output():
    message = AIMessage(
        content='{"message":"ok"}',
        usage_metadata={
            "input_tokens": 321,
            "output_tokens": 45,
            "total_tokens": 366,
        },
    )

    output = model_output_from_ai_message(message)

    assert output["token_usage"] == {
        "input_tokens": 321,
        "output_tokens": 45,
    }


async def test_model_call_observation_keeps_decision_evidence_internal():
    class FakeModel:
        model = "test-model"
        temperature = 0.2

        async def ainvoke(self, _messages):
            return AIMessage(
                content='{"message":"private output"}',
                usage_metadata={
                    "input_tokens": 21,
                    "output_tokens": 7,
                    "total_tokens": 28,
                },
            )

    observations: list[dict] = []
    with capture_model_calls(observations):
        await traced_model_ainvoke(
            FakeModel(),
            [AIMessage(content="private input")],
            name="test.generation",
            prompt_version_id="prompt-v1",
        )

    assert len(observations) == 1
    observation = observations[0]
    assert observation["observation_type"] == "generation"
    assert observation["usage_details"] == {
        "input": 21,
        "output": 7,
    }
    assert observation["decision_evidence"]["public_output"] == (
        '{"message":"private output"}'
    )
    assert observation["decision_evidence"][
        "private_reasoning"
    ]["retained"] is False
    attached = response_with_model_calls(
        AgentResponse(
            trace_id="trace-model-call",
            agent_name="test",
            prompt_version_id="prompt-v1",
            decision_type="test",
            structured_payload={},
            human_summary="ok",
        ),
        observations,
    )
    assert attached.structured_payload["token_usage"] == {
        "input_tokens": 21,
        "output_tokens": 7,
    }
    assert "decision_evidence" not in str(
        attached.structured_payload[
            "model_call_observations"
        ]
    )


async def test_trace_encrypts_selection_and_model_decision_evidence():
    store, factory = _store()
    trace_id = "trace-encrypted-decision-evidence"
    request = ChatSyncRequest(
        request_id="req_0000000000000101",
        message_id="user_msg_0000000000000101",
        patient_id="patient_0000000000000101",
        requested_return_type="selection_box",
        message="마카롱_호박고구마 마카롱",
        message_at=datetime(
            2026,
            7,
            30,
            9,
            30,
            tzinfo=timezone.utc,
        ),
    )
    observations: list[dict] = []

    class FoodSelectionModel:
        model = "test-model"

        async def ainvoke(self, _messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": (
                            "search_nutrition_food_candidates"
                        ),
                        "args": {
                            "query": (
                                "마카롱_호박고구마 마카롱"
                            ),
                            "limit": 6,
                        },
                        "id": "tool-food-search",
                    }
                ],
            )

    with capture_model_calls(observations):
        await traced_model_ainvoke(
            FoodSelectionModel(),
            [
                HumanMessage(
                    content=(
                        '{"message":"마카롱_호박고구마 마카롱",'
                        '"context":{"structured_response_context":'
                        '{"response_type":"selection_box",'
                        '"response_value":"마카롱_호박고구마 마카롱",'
                        '"source_message":{"message_type":'
                        '"selection_box","message":{"selections":'
                        '["마카롱_호박고구마 마카롱"]}}}}}'
                    )
                )
            ],
            name="nutrition_management_agent.tool_loop",
            prompt_version_id="prompt-v1",
        )

    store.start_chat(
        request,
        trace_id=trace_id,
        api_path="/agent/sync/chat",
    )
    response = response_with_model_calls(
        AgentResponse(
            trace_id=trace_id,
            agent_name="nutrition_management_agent",
            prompt_version_id="prompt-v1",
            decision_type="tool_call",
            structured_payload={},
            human_summary="후보를 확인했습니다.",
        ),
        observations,
    )
    store.complete_chat(
        request,
        response,
        model_call_observations=observations,
    )

    with factory() as session:
        steps = list(
            session.scalars(
                select(AgentRunStep)
                .where(AgentRunStep.trace_id == trace_id)
                .order_by(AgentRunStep.sequence)
            )
        )

    assert len(steps) == 3
    assert all(step.evidence_ciphertext for step in steps)
    assert all(step.evidence_hash for step in steps)
    assert all(
        "마카롱_호박고구마" not in step.metadata_json
        for step in steps
    )
    ingress = steps[0]
    ingress_evidence = store.evidence_cipher.decrypt_json(
        ingress.evidence_ciphertext,
        context=TraceEvidenceContext(
            trace_id=ingress.trace_id,
            observation_id=ingress.observation_id,
            step_type=ingress.step_type,
            sequence=ingress.sequence,
            trace_attempt_number=(
                ingress.trace_attempt_number
            ),
        ),
    )
    assert ingress_evidence["message"] == (
        "마카롱_호박고구마 마카롱"
    )
    model_step = next(
        step for step in steps
        if step.step_type == "model_call"
    )
    model_evidence = store.evidence_cipher.decrypt_json(
        model_step.evidence_ciphertext,
        context=TraceEvidenceContext(
            trace_id=model_step.trace_id,
            observation_id=model_step.observation_id,
            step_type=model_step.step_type,
            sequence=model_step.sequence,
            trace_attempt_number=(
                model_step.trace_attempt_number
            ),
        ),
    )
    assert model_evidence["system_reason_code"] == (
        "STRUCTURED_SELECTION_AUTHORITATIVE_FOOD_LOOKUP"
    )
    assert model_evidence["tool_calls"][0]["arguments"][
        "query"
    ] == "마카롱_호박고구마 마카롱"


def test_trace_retries_append_observations_and_keep_field_semantics():
    store, factory = _store()
    trace_id = "async-trace-append-only"
    payload = {"patient_id": "patient-private"}

    def response(summary: str) -> AgentResponse:
        return AgentResponse(
            trace_id=trace_id,
            agent_name="missed_dose_coach",
            prompt_version_id="missed_dose_generation_v1",
            decision_type="missed_dose_assessment",
            structured_payload={
                "final_answer_source": "llm_generation",
                "model_call_observations": [
                    {
                        "observation_id": f"model-{summary}",
                        "observation_type": "generation",
                        "name": "missed_dose_generation",
                        "status": "COMPLETED",
                        "prompt_version_id": (
                            "missed_dose_generation_v1"
                        ),
                        "provider": "bedrock",
                        "model_id": "test-model",
                        "input_hash": "a" * 64,
                        "output_hash": "b" * 64,
                        "usage_details": {
                            "input": 10,
                            "output": 2,
                        },
                        "model_parameters": {"temperature": 0.2},
                        "started_at": "2026-07-29T09:00:00",
                        "completed_at": "2026-07-29T09:00:00.010",
                        "latency_ms": 10,
                    }
                ],
                "tool_calls": [
                    {
                        "id": "same-call-id",
                        "name": "get_medication_dose_status",
                        "arguments": {"status": "missed"},
                    }
                ],
                "tool_results": [
                    {
                        "tool_name": "get_medication_dose_status",
                        "status": "success",
                        "response": {"status": "missed"},
                        "elapsed_ms": 17,
                        "attempt_count": 2,
                        "retryable": False,
                    }
                ],
            },
            human_summary=summary,
        )

    store.start_async_task(
        request_id="request-append-only",
        task_type="missed_dose",
        payload=payload,
        trace_id=trace_id,
    )
    store.complete_async_task(
        request_id="request-append-only",
        patient_id="patient-private",
        response=response("attempt-one"),
    )
    store.start_async_task(
        request_id="request-append-only",
        task_type="missed_dose",
        payload=payload,
        trace_id=trace_id,
    )
    store.complete_async_task(
        request_id="request-append-only",
        patient_id="patient-private",
        response=response("attempt-two"),
    )

    with factory() as session:
        trace = session.scalar(
            select(AgentRunTrace).where(
                AgentRunTrace.trace_id == trace_id
            )
        )
        steps = list(
            session.scalars(
                select(AgentRunStep)
                .where(AgentRunStep.trace_id == trace_id)
                .order_by(AgentRunStep.sequence)
            )
        )
        executions = list(
            session.scalars(
                select(AgentToolExecution)
                .where(AgentToolExecution.trace_id == trace_id)
                .order_by(AgentToolExecution.id)
            )
        )

    assert trace is not None
    assert trace.attempt_count == 2
    assert trace.final_answer_source == "llm_generation"
    assert trace.input_tokens == 10
    assert trace.output_tokens == 2
    assert len(steps) == 8
    assert [step.trace_attempt_number for step in steps] == [
        1,
        1,
        1,
        1,
        2,
        2,
        2,
        2,
    ]
    assert [
        step.observation_type
        for step in steps
        if step.step_type == "model_call"
    ] == ["generation", "generation"]
    assert len(executions) == 2
    assert [row.trace_attempt_number for row in executions] == [1, 2]
    assert all(row.latency_ms == 17 for row in executions)
    assert all(row.attempt_count == 2 for row in executions)
