from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import agent_app.persistence.trace_store as trace_store_module
from agent_app.llm.messages import model_output_from_ai_message
from agent_app.observability.evidence import TraceEvidenceContext
from agent_app.observability.model_calls import (
    capture_model_calls,
    record_embedding_observation,
    response_with_model_calls,
    traced_model_ainvoke,
)
from agent_app.persistence.models import (
    AgentRunStep,
    AgentRunTrace,
    AgentToolExecution,
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
        kwargs = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_medication_dose_status",
                        "description": "Read dose status",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                        },
                    },
                }
            ]
        }
        additional_model_request_fields = {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"},
        }

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
            [
                SystemMessage(content="system routing prompt"),
                HumanMessage(
                    content=json.dumps(
                        {
                            "message": "private input",
                            "context": {
                                "patient_context_snapshot": {
                                    "medication_count": 4,
                                }
                            },
                        }
                    )
                ),
            ],
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
    assert observation["model_parameters"]["thinking_type"] == (
        "adaptive"
    )
    assert observation["model_parameters"]["reasoning_effort"] == (
        "medium"
    )
    assert observation["decision_evidence"]["public_output"] == (
        '{"message":"private output"}'
    )
    assert observation["decision_evidence"][
        "private_reasoning"
    ]["retained"] is False
    assert observation["model_call_index"] == 1
    assert observation["route_decision"] == {
        "reason_code": "MODEL_FINAL_RESPONSE",
        "outcome": "final_response",
        "selected_tool_names": [],
    }
    composition = observation["input_composition"]
    assert composition["measurement_version"] == (
        "input_composition_v1"
    )
    assert composition["provider_input_tokens"] == 21
    assert composition["message_count"] == 2
    assert composition["components"]["system_prompt"][
        "characters"
    ] == len("system routing prompt")
    assert composition["payload_components"]["context"][
        "characters"
    ] > 0
    assert composition["tool_schema"]["count"] == 1
    assert composition["tool_schema"]["estimated_tokens"] > 0
    assert "private input" not in json.dumps(composition)
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


def test_embedding_observation_is_persisted_as_embedding_step():
    store, factory = _store()
    trace_id = "trace-embedding-observation"
    request = ChatSyncRequest(
        request_id="req_0000000000000191",
        message_id="user_msg_0000000000000191",
        patient_id="patient_0000000000000191",
        requested_return_type="text",
        message="비식별 테스트 문장",
        message_at=datetime(2026, 8, 2, 9, 30, tzinfo=UTC),
    )
    observations: list[dict] = []
    started_at = datetime(2026, 8, 2, 9, 30, tzinfo=UTC)
    with capture_model_calls(observations):
        record_embedding_observation(
            name="cohere_reference_embedding",
            provider="bedrock_cohere",
            model_id="cohere.embed-multilingual-v3",
            region="ap-northeast-1",
            input_type="search_query",
            texts=["비식별 테스트 문장"],
            dimensions=1024,
            embeddings=[[0.0] * 1024],
            started_at=started_at,
            completed_at=started_at,
            latency_ms=12,
            status="COMPLETED",
        )

    store.start_chat(request, trace_id=trace_id, api_path="/agent/sync/chat")
    store.record_model_calls(
        trace_id=trace_id,
        observations=observations,
    )

    with factory() as session:
        step = session.scalar(
            select(AgentRunStep).where(
                AgentRunStep.trace_id == trace_id,
                AgentRunStep.step_type == "embedding",
            )
        )

    assert step is not None
    assert step.observation_type == "embedding"
    assert step.provider == "bedrock_cohere"
    assert step.model_id == "cohere.embed-multilingual-v3"
    assert "비식별 테스트 문장" not in step.metadata_json


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
            tzinfo=UTC,
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
                            "food_queries": [
                                (
                                    "마카롱_호박고구마 "
                                    "마카롱"
                                )
                            ],
                            "limit_per_query": 6,
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
            structured_payload={"routing_mode": "delegated_agent"},
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
        "food_queries"
    ] == ["마카롱_호박고구마 마카롱"]
    model_metadata = json.loads(model_step.metadata_json)
    assert model_metadata["model_call_index"] == 1
    assert model_metadata["workflow_route"] == "delegated_agent"
    assert model_metadata["decision_reason_code"] == (
        "STRUCTURED_SELECTION_AUTHORITATIVE_FOOD_LOOKUP"
    )
    assert model_metadata["route_decision"]["outcome"] == (
        "tool_call"
    )
    assert model_metadata["input_composition"][
        "measurement_version"
    ] == "input_composition_v1"


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


def test_trace_uses_runtime_tool_observation_after_response_overlay():
    store, factory = _store()
    trace_id = "trace-runtime-tool-observation"
    request = ChatSyncRequest(
        request_id="req_0000000000000201",
        message_id="user_msg_0000000000000201",
        patient_id="patient_0000000000000201",
        requested_return_type="text",
        message="survey response",
        message_at=datetime(2026, 7, 31, 9, 30, tzinfo=UTC),
    )
    started_at = datetime(2026, 7, 31, 9, 30, 1)
    completed_at = datetime(2026, 7, 31, 9, 30, 1, 23_000)
    tool_observations = [
        {
            "observation_id": "runtime-tool-observation-1",
            "call": {
                "id": "runtime-tool-call-1",
                "name": "get_pro_ctcae_questionnaire",
                "arguments": {"symptom_text": "private symptom"},
            },
            "result": {
                "tool_name": "get_pro_ctcae_questionnaire",
                "status": "success",
                "response": {"matched": True},
                "elapsed_ms": 23,
                "attempt_count": 1,
                "retryable": False,
            },
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "latency_ms": 23,
        }
    ]
    store.start_chat(
        request,
        trace_id=trace_id,
        api_path="/agent/sync/chat",
    )
    store.complete_chat(
        request,
        AgentResponse(
            trace_id=trace_id,
            agent_name="pro_ctcae_survey_state",
            prompt_version_id="pro_ctcae_survey_v1",
            decision_type="pro_ctcae_questionnaire",
            structured_payload={
                "final_answer_source": "deterministic_survey_state",
            },
            human_summary="survey question",
        ),
        tool_call_observations=tool_observations,
    )

    with factory() as session:
        trace = session.scalar(
            select(AgentRunTrace).where(
                AgentRunTrace.trace_id == trace_id
            )
        )
        execution = session.scalar(
            select(AgentToolExecution).where(
                AgentToolExecution.trace_id == trace_id
            )
        )
        step = session.scalar(
            select(AgentRunStep).where(
                AgentRunStep.trace_id == trace_id,
                AgentRunStep.step_type == "tool_call",
            )
        )

    assert trace is not None
    assert trace.tool_count == 1
    assert json.loads(trace.metadata_json)["tool_timing_source"] == (
        "runtime_observation"
    )
    assert execution is not None
    assert execution.started_at == started_at
    assert execution.completed_at == completed_at
    assert execution.latency_ms == 23
    assert step is not None
    assert step.observation_id == "runtime-tool-observation-1"
    assert step.started_at == started_at
    assert step.completed_at == completed_at


def test_failed_trace_persists_write_tool_runtime_observation() -> None:
    store, factory = _store()
    trace_id = "trace-failed-write-tool-observation"
    request = ChatSyncRequest(
        request_id="req_0000000000000202",
        message_id="user_msg_0000000000000202",
        patient_id="patient_0000000000000202",
        requested_return_type="text",
        message="변경",
        message_at=datetime(2026, 7, 31, 9, 30, tzinfo=UTC),
    )
    observation = {
        "observation_id": "failed-write-tool-observation-1",
        "call": {
            "id": "failed-write-tool-call-1",
            "name": "change_notification_policy",
            "arguments": {
                "approval_key": "private-approval-key",
            },
        },
        "result": {
            "tool_name": "change_notification_policy",
            "status": "error",
            "response": {
                "request_id": "private-backend-request-id",
            },
            "error": "POLICY_EFFECTIVE_DATE_RANGE_TOO_LARGE",
            "elapsed_ms": 102,
            "attempt_count": 1,
            "retryable": False,
        },
        "started_at": "2026-07-31T09:30:01",
        "completed_at": "2026-07-31T09:30:01.102000",
        "latency_ms": 102,
    }
    store.start_chat(
        request,
        trace_id=trace_id,
        api_path="/agent/sync/chat",
    )
    store.fail_chat(
        trace_id=trace_id,
        error_code="TOOL_EXECUTION_FAILED",
        error_message="write tool failed",
        retryable=False,
        tool_call_observations=[observation],
    )

    with factory() as session:
        trace = session.scalar(
            select(AgentRunTrace).where(
                AgentRunTrace.trace_id == trace_id
            )
        )
        execution = session.scalar(
            select(AgentToolExecution).where(
                AgentToolExecution.trace_id == trace_id
            )
        )
        steps = list(
            session.scalars(
                select(AgentRunStep)
                .where(AgentRunStep.trace_id == trace_id)
                .order_by(AgentRunStep.sequence)
            ).all()
        )

    assert trace is not None
    assert trace.status == "FINAL_FAILED"
    assert trace.tool_count == 1
    assert json.loads(trace.metadata_json)["tool_timing_source"] == (
        "runtime_observation"
    )
    assert execution is not None
    assert execution.tool_name == "change_notification_policy"
    assert execution.status == "ERROR"
    assert execution.error_code == (
        "POLICY_EFFECTIVE_DATE_RANGE_TOO_LARGE"
    )
    assert execution.latency_ms == 102
    assert [step.step_type for step in steps][-2:] == [
        "tool_call",
        "final_response",
    ]
    tool_step = steps[-2]
    assert tool_step.observation_id == "failed-write-tool-observation-1"
    assert tool_step.status == "ERROR"
    assert "private-approval-key" not in tool_step.metadata_json
    assert "private-backend-request-id" not in tool_step.metadata_json
