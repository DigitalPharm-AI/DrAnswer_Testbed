from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from agent_app.observability.langfuse_exporter import (
    LangfuseExportError,
    LangfuseHttpExporter,
    build_otlp_trace_payload,
)
from agent_app.observability.outbox import (
    DEAD,
    RETRYABLE_FAILED,
    SCORE,
    TRACE_ATTEMPT,
    ObservabilityOutbox,
    deterministic_langfuse_trace_id,
    enqueue_user_feedback_score,
)
from agent_app.persistence.models import (
    AgentObservabilityExport,
    AgentRunStep,
    AgentRunTrace,
)
import agent_app.persistence.retention as retention_module
from agent_app.persistence.retention import purge_expired_agent_state
from agent_app.persistence.trace_store import AgentTraceStore
from shared.schemas import AgentResponse
from shared.settings import Settings
from shared.time_utils import utc_now
from tests.helpers import build_agent_engine


def _settings(*, max_attempts: int = 3) -> Settings:
    return Settings(
        app_env="test",
        app_release_version="test-release",
        llm_provider="bedrock_anthropic",
        llm_model_tier="sonnet",
        agent_cost_input_usd_per_1m_tokens=2.0,
        agent_cost_output_usd_per_1m_tokens=4.0,
        langfuse_export_enabled=True,
        langfuse_base_url="https://langfuse.internal",
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key=SecretStr("sk-lf-test"),
        langfuse_export_max_attempts=max_attempts,
        langfuse_export_retry_base_seconds=1,
        langfuse_export_retry_max_seconds=4,
        langfuse_success_sample_rate=1.0,
    )


def _store(
    *,
    max_attempts: int = 3,
) -> tuple[AgentTraceStore, sessionmaker, Settings]:
    engine, _cleanup = build_agent_engine("langfuse_exporter")
    factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        future=True,
    )
    settings = _settings(max_attempts=max_attempts)
    return AgentTraceStore(factory, settings=settings), factory, settings


def _response(trace_id: str, summary: str) -> AgentResponse:
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
                    "prompt_version_id": "missed_dose_generation_v1",
                    "provider": "bedrock",
                    "model_id": "test-model",
                    "input_hash": "a" * 64,
                    "output_hash": "b" * 64,
                    "usage_details": {"input": 10, "output": 2},
                    "cost_details": {"total": 0.000028},
                    "model_parameters": {"temperature": 0.2},
                    "started_at": "2026-07-29T09:00:00",
                    "completed_at": "2026-07-29T09:00:00.010",
                    "latency_ms": 10,
                }
            ],
        },
        human_summary=summary,
    )


def test_outbox_keeps_retries_in_one_trace_and_stages_scores():
    store, factory, _settings_value = _store()
    trace_id = "trace-one-logical-item"
    payload = {"patient_id": "patient-private"}

    store.start_async_task(
        request_id="request-one",
        task_type="missed_dose",
        payload=payload,
        trace_id=trace_id,
    )
    store.fail_chat(
        trace_id=trace_id,
        error_code="PROVIDER_ERROR",
        error_message="provider failure",
        retryable=True,
    )
    store.start_async_task(
        request_id="request-one",
        task_type="missed_dose",
        payload=payload,
        trace_id=trace_id,
    )
    store.complete_async_task(
        request_id="request-one",
        patient_id="patient-private",
        response=_response(trace_id, "safe summary"),
    )

    with factory() as session:
        rows = list(
            session.scalars(
                select(AgentObservabilityExport).order_by(
                    AgentObservabilityExport.id
                )
            ).all()
        )

    trace_rows = [row for row in rows if row.event_type == TRACE_ATTEMPT]
    score_rows = [row for row in rows if row.event_type == SCORE]
    assert [row.trace_attempt_number for row in trace_rows] == [1, 2]
    assert len({row.remote_trace_id for row in trace_rows}) == 1
    assert trace_rows[0].remote_trace_id == (
        deterministic_langfuse_trace_id(trace_id)
    )
    assert {
        json.loads(row.payload_json)["name"]
        for row in score_rows
    } >= {
        "agent_success",
        "response_validation",
        "retry_count",
    }
    retry_scores = [
        json.loads(row.payload_json)
        for row in score_rows
        if json.loads(row.payload_json)["name"] == "retry_count"
    ]
    assert [score["value"] for score in retry_scores] == [0.0, 1.0]
    assert len({score["id"] for score in retry_scores}) == 1


def test_otlp_payload_preserves_timing_tree_usage_and_redacts_content():
    store, factory, _settings_value = _store()
    trace_id = "trace-otlp-payload"
    store.start_async_task(
        request_id="request-private",
        task_type="missed_dose",
        payload={
            "patient_id": "patient-private",
            "symptom": "raw private symptom",
        },
        trace_id=trace_id,
    )
    store.complete_async_task(
        request_id="request-private",
        patient_id="patient-private",
        response=_response(trace_id, "raw private answer"),
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
            ).all()
        )
    assert trace is not None
    payload = build_otlp_trace_payload(
        trace,
        steps,
        trace_attempt_number=1,
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert all(span["traceId"] == spans[0]["traceId"] for span in spans)
    assert len(spans[0]["traceId"]) == 32
    assert all(len(span["spanId"]) == 16 for span in spans)
    assert all(
        int(span["endTimeUnixNano"]) > int(span["startTimeUnixNano"])
        for span in spans
    )
    assert any(
        attribute["key"] == "langfuse.observation.usage_details"
        for span in spans
        for attribute in span["attributes"]
    )
    observation_types = {
        attribute["value"]["stringValue"]
        for span in spans
        for attribute in span["attributes"]
        if attribute["key"] == "langfuse.observation.type"
    }
    assert observation_types <= {"span", "generation", "event"}
    assert "generation" in observation_types
    assert "patient-private" not in serialized
    assert "raw private symptom" not in serialized
    assert "raw private answer" not in serialized
    assert trace.patient_id_hash in serialized


def test_acknowledged_http_failure_retries_then_dead_and_feedback_is_safe():
    store, factory, settings = _store(max_attempts=2)
    trace_id = "trace-http-retry"
    store.start_async_task(
        request_id="request-http",
        task_type="missed_dose",
        payload={"patient_id": "patient-private"},
        trace_id=trace_id,
    )
    store.complete_async_task(
        request_id="request-http",
        patient_id="patient-private",
        response=_response(trace_id, "answer"),
    )

    with factory() as session:
        enqueue_user_feedback_score(
            session,
            request_id="feedback-request",
            message_id="assistant-message",
            patient_id_hash="c" * 64,
            trace_id=trace_id,
            feedback=False,
            feedback_at=utc_now(),
            feedback_text_present=True,
            settings=settings,
        )
        session.commit()
        feedback_row = session.scalar(
            select(AgentObservabilityExport).where(
                AgentObservabilityExport.event_key
                == "score:user_feedback:feedback-request"
            )
        )
        assert feedback_row is not None
        assert "feedback_text" not in feedback_row.payload_json
        assert "assistant-message" not in feedback_row.payload_json
        trace_export = session.scalar(
            select(AgentObservabilityExport).where(
                AgentObservabilityExport.event_type == TRACE_ATTEMPT
            )
        )
        assert trace_export is not None
        for row in session.scalars(
            select(AgentObservabilityExport).where(
                AgentObservabilityExport.id != trace_export.id
            )
        ):
            row.status = "COMPLETED"
            row.next_attempt_at = None
        session.commit()

    outbox = ObservabilityOutbox(factory, settings=settings)
    first = outbox.claim_due(worker_id="test-worker", limit=1)[0]

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(unavailable))
    exporter = LangfuseHttpExporter(
        factory,
        settings=settings,
        client=client,
    )
    with pytest.raises(LangfuseExportError) as exc_info:
        exporter.export(first)
    assert exc_info.value.retryable is True
    assert outbox.fail(
        first.id,
        error_code=exc_info.value.code,
        error_message=str(exc_info.value),
        retryable=True,
    )

    with factory() as session:
        row = session.get(AgentObservabilityExport, first.id)
        assert row is not None
        assert row.status == RETRYABLE_FAILED
        retry_at = row.next_attempt_at
    assert retry_at is not None

    second = outbox.claim_due(
        worker_id="test-worker",
        limit=1,
        now=retry_at + timedelta(seconds=1),
    )[0]
    with pytest.raises(LangfuseExportError) as exc_info:
        exporter.export(second)
    assert outbox.fail(
        second.id,
        error_code=exc_info.value.code,
        error_message=str(exc_info.value),
        retryable=True,
        now=retry_at + timedelta(seconds=1),
    ) is False
    with factory() as session:
        row = session.get(AgentObservabilityExport, second.id)
        assert row is not None
        assert row.status == DEAD
        assert row.attempt_count == 2
    client.close()


def test_trace_retention_atomically_stages_remote_delete(monkeypatch):
    store, factory, settings = _store()
    trace_id = "trace-retention-delete"
    store.start_async_task(
        request_id="request-retention-delete",
        task_type="missed_dose",
        payload={"patient_id": "patient-private"},
        trace_id=trace_id,
    )
    store.complete_async_task(
        request_id="request-retention-delete",
        patient_id="patient-private",
        response=_response(trace_id, "answer"),
    )
    with factory() as session:
        trace = session.scalar(
            select(AgentRunTrace).where(
                AgentRunTrace.trace_id == trace_id
            )
        )
        assert trace is not None
        expiry = trace.expires_at
        for row in session.scalars(
            select(AgentObservabilityExport)
        ):
            row.status = "COMPLETED"
            row.next_attempt_at = None
        session.commit()

    monkeypatch.setattr(
        retention_module,
        "get_settings",
        lambda: settings,
    )
    result = purge_expired_agent_state(
        factory,
        now=expiry + timedelta(seconds=1),
    )

    with factory() as session:
        assert session.scalar(
            select(AgentRunTrace).where(
                AgentRunTrace.trace_id == trace_id
            )
        ) is None
        delete_row = session.scalar(
            select(AgentObservabilityExport).where(
                AgentObservabilityExport.event_key
                == f"delete:{trace_id}"
            )
        )
        assert delete_row is not None
        assert delete_row.status == "PENDING"
        assert json.loads(delete_row.payload_json)["traceId"] == (
            deterministic_langfuse_trace_id(trace_id)
        )
    assert result["deleted_run_traces"] == 1
