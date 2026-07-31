from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent_app.observability.evidence import (
    TraceEvidenceCipher,
    TraceEvidenceContext,
)
from agent_app.observability.outbox import enqueue_trace_attempt
from shared.chat_contracts import ChatSyncRequest
from agent_app.persistence.models import AgentRunStep, AgentRunTrace, AgentToolExecution
from shared.tool_names import MODEL_VISIBLE_TOOL_METADATA
from shared.json_utils import dump_json
from shared.readiness_budget import estimate_model_cost_usd
from shared.redaction import redacted_clinical_text_label
from shared.retention_policy import agent_observability_expires_at
from shared.schemas import AgentResponse
from shared.settings import Settings, get_settings
from shared.time_utils import utc_now


class AgentTraceStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.evidence_cipher = TraceEvidenceCipher.from_settings(
            self.settings
        )

    def start_chat(
        self,
        request: ChatSyncRequest,
        *,
        trace_id: str,
        api_path: str,
    ) -> None:
        now = utc_now()
        expires_at = agent_observability_expires_at(now)
        with self.session_factory() as session:
            trace = session.scalar(
                select(AgentRunTrace)
                .where(AgentRunTrace.trace_id == trace_id)
                .with_for_update()
            )
            if trace is None:
                trace = AgentRunTrace(
                    trace_id=trace_id,
                    request_id=request.request_id,
                    api_path=api_path,
                    message_id=request.message_id,
                    patient_id_hash=_sha256_text(request.patient_id),
                    environment=self.settings.app_env,
                    release_version=self.settings.app_release_version,
                    workflow_name="multiturn_chat",
                    status="PROCESSING",
                    attempt_count=1,
                    provider=self.settings.llm_provider,
                    model_tier=self.settings.llm_model_tier,
                    model_id=self.settings.model_id_for_tier(),
                    input_hash=_sha256_text(request.message),
                    metadata_json=dump_json(
                        {
                            "requested_return_type": request.requested_return_type,
                            "message_at": request.message_at.isoformat(),
                        }
                    ),
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                )
                session.add(trace)
                # AgentRunStep references trace_id. Without an ORM relationship,
                # SQLAlchemy may otherwise flush the step before its new parent
                # when PostgreSQL foreign keys are enforced.
                session.flush()
            else:
                trace.attempt_count += 1
                trace.status = "PROCESSING"
                trace.output_hash = ""
                trace.input_tokens = 0
                trace.output_tokens = 0
                trace.estimated_cost_usd = 0.0
                trace.latency_ms = 0
                trace.tool_count = 0
                trace.error_code = ""
                trace.error_message = ""
                trace.completed_at = None
                trace.updated_at = now
                trace.expires_at = expires_at
            sequence = _next_sequence(session, trace_id)
            observation_id = _observation_id()
            evidence = self._encrypted_evidence_fields(
                trace_id=trace_id,
                observation_id=observation_id,
                step_type="request_ingress",
                sequence=sequence,
                trace_attempt_number=trace.attempt_count,
                value={
                    "evidence_version": "v1",
                    "event_type": "request_ingress",
                    "request_id": request.request_id,
                    "message_id": request.message_id,
                    "requested_return_type": (
                        request.requested_return_type
                    ),
                    "message": request.message,
                    "message_at": request.message_at.isoformat(),
                },
            )
            session.add(
                AgentRunStep(
                    trace_id=trace_id,
                    sequence=sequence,
                    observation_id=observation_id,
                    observation_type="agent",
                    trace_attempt_number=trace.attempt_count,
                    step_type="request_ingress",
                    step_name="sync_chat",
                    status="COMPLETED",
                    started_at=now,
                    completed_at=now,
                    created_at=now,
                    expires_at=expires_at,
                    metadata_json=dump_json(
                        {
                            "api_path": api_path,
                            "decision_evidence_encrypted": True,
                            "langfuse_trace_name": "multiturn_chat",
                        }
                    ),
                    **evidence,
                )
            )
            session.commit()

    def ensure_chat_started(
        self,
        request: ChatSyncRequest,
        *,
        trace_id: str,
        api_path: str,
    ) -> None:
        """Create a preflight Trace without incrementing an existing attempt."""

        with self.session_factory() as session:
            if session.scalar(
                select(AgentRunTrace.id).where(
                    AgentRunTrace.trace_id == trace_id
                )
            ) is not None:
                return
        self.start_chat(
            request,
            trace_id=trace_id,
            api_path=api_path,
        )

    def start_async_task(
        self,
        *,
        request_id: str,
        task_type: str,
        payload: dict[str, Any],
        trace_id: str,
    ) -> None:
        """Start one background Agent run without retaining clinical payloads."""

        now = utc_now()
        expires_at = agent_observability_expires_at(now)
        patient_id = str(payload.get("patient_id") or "")
        with self.session_factory() as session:
            trace = session.scalar(
                select(AgentRunTrace)
                .where(AgentRunTrace.trace_id == trace_id)
                .with_for_update()
            )
            if trace is None:
                trace = AgentRunTrace(
                    trace_id=trace_id,
                    request_id=request_id,
                    api_path=f"/agent/async/{task_type}",
                    message_id=str(payload.get("message_id") or ""),
                    patient_id_hash=_sha256_text(patient_id),
                    environment=self.settings.app_env,
                    release_version=self.settings.app_release_version,
                    workflow_name=task_type,
                    status="PROCESSING",
                    attempt_count=1,
                    provider=self.settings.llm_provider,
                    model_tier=self.settings.llm_model_tier,
                    model_id=self.settings.model_id_for_tier(),
                    input_hash=_sha256_json(payload),
                    metadata_json=dump_json(
                        {
                            "execution_mode": "async",
                            "payload_keys": sorted(
                                str(key)
                                for key in payload
                                if not str(key).startswith("_")
                            ),
                        }
                    ),
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                )
                session.add(trace)
                session.flush()
            else:
                trace.attempt_count += 1
                trace.status = "PROCESSING"
                trace.output_hash = ""
                trace.input_tokens = 0
                trace.output_tokens = 0
                trace.estimated_cost_usd = 0.0
                trace.latency_ms = 0
                trace.tool_count = 0
                trace.error_code = ""
                trace.error_message = ""
                trace.completed_at = None
                trace.updated_at = now
                trace.expires_at = expires_at
            sequence = _next_sequence(session, trace_id)
            observation_id = _observation_id()
            evidence = self._encrypted_evidence_fields(
                trace_id=trace_id,
                observation_id=observation_id,
                step_type="request_ingress",
                sequence=sequence,
                trace_attempt_number=trace.attempt_count,
                value={
                    "evidence_version": "v1",
                    "event_type": "async_request_ingress",
                    "request_id": request_id,
                    "task_type": task_type,
                    "payload": payload,
                },
            )
            session.add(
                AgentRunStep(
                    trace_id=trace_id,
                    sequence=sequence,
                    observation_id=observation_id,
                    observation_type="agent",
                    trace_attempt_number=trace.attempt_count,
                    step_type="request_ingress",
                    step_name=task_type,
                    status="COMPLETED",
                    started_at=now,
                    completed_at=now,
                    created_at=now,
                    expires_at=expires_at,
                    metadata_json=dump_json(
                        {
                            "execution_mode": "async",
                            "decision_evidence_encrypted": True,
                            "langfuse_trace_name": task_type,
                        }
                    ),
                    **evidence,
                )
            )
            session.commit()

    def complete_chat(
        self,
        request: ChatSyncRequest,
        response: AgentResponse,
        *,
        model_call_observations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._complete_trace(
            request_id=request.request_id,
            patient_id_hash=_sha256_text(request.patient_id),
            response=response,
            model_call_observations=model_call_observations,
        )

    def complete_async_task(
        self,
        *,
        request_id: str,
        patient_id: str,
        response: AgentResponse,
        model_call_observations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._complete_trace(
            request_id=request_id,
            patient_id_hash=_sha256_text(patient_id),
            response=response,
            model_call_observations=model_call_observations,
        )

    def _complete_trace(
        self,
        *,
        request_id: str,
        patient_id_hash: str,
        response: AgentResponse,
        model_call_observations: list[dict[str, Any]] | None = None,
    ) -> None:
        now = utc_now()
        trace_expires_at = agent_observability_expires_at(now)
        tool_expires_at = trace_expires_at
        structured = response.structured_payload if isinstance(response.structured_payload, dict) else {}
        token_usage = _token_usage(structured)
        tool_calls = _dict_list(structured.get("tool_calls"))
        tool_results = _dict_list(structured.get("tool_results"))
        model_calls = (
            [
                dict(observation)
                for observation in model_call_observations
                if isinstance(observation, dict)
            ]
            if model_call_observations is not None
            else _dict_list(
                structured.get("model_call_observations")
            )
        )

        with self.session_factory() as session:
            trace = session.scalar(
                select(AgentRunTrace)
                .where(AgentRunTrace.trace_id == response.trace_id)
                .with_for_update()
            )
            if trace is None:
                raise RuntimeError("agent_trace_not_started")

            trace.status = "COMPLETED"
            trace.agent_name = response.agent_name
            trace.decision_type = response.decision_type
            trace.route = str(structured.get("routing_mode") or "")
            trace.final_answer_source = str(
                structured.get("final_answer_source") or ""
            )
            trace.prompt_version_id = response.prompt_version_id
            last_model_call = model_calls[-1] if model_calls else {}
            trace.provider = str(
                last_model_call.get("provider")
                or structured.get("provider")
                or self.settings.llm_provider
            )
            trace.model_tier = str(structured.get("model_tier") or self.settings.llm_model_tier)
            trace.model_id = str(
                last_model_call.get("model_id")
                or
                structured.get("model_id")
                or self.settings.model_id_for_tier(trace.model_tier)
            )
            trace.output_hash = _sha256_text(response.human_summary)
            trace.input_tokens = token_usage["input_tokens"]
            trace.output_tokens = token_usage["output_tokens"]
            trace.estimated_cost_usd = estimate_model_cost_usd(
                input_tokens=trace.input_tokens,
                output_tokens=trace.output_tokens,
                input_usd_per_1m_tokens=self.settings.agent_cost_input_usd_per_1m_tokens,
                output_usd_per_1m_tokens=self.settings.agent_cost_output_usd_per_1m_tokens,
            )
            attempt_started_at = _attempt_started_at(
                session,
                response.trace_id,
                trace.attempt_count,
            )
            trace.latency_ms = _nonnegative_int(
                structured.get("elapsed_ms") or structured.get("latency_ms")
            ) or _duration_ms(attempt_started_at, now)
            trace.tool_count = len(tool_calls)
            trace.error_code = ""
            trace.error_message = ""
            trace.metadata_json = dump_json(
                {
                    **_json_object(trace.metadata_json),
                    "validation_passed": response.validation_passed,
                    "validation_error_count": len(response.validation_errors),
                    "structured_payload_keys": sorted(str(key) for key in structured),
                    "requires_conversation_alert": response.requires_conversation_alert,
                    "langfuse_tags": [
                        trace.workflow_name,
                        response.agent_name,
                        response.decision_type,
                    ],
                }
            )
            trace.completed_at = now
            trace.updated_at = now
            trace.expires_at = trace_expires_at

            sequence = _next_sequence(
                session,
                response.trace_id,
            )
            parent_observation_id = _attempt_root_observation_id(
                session,
                response.trace_id,
                trace.attempt_count,
            )
            for observation in model_calls:
                self._add_model_trace(
                    session,
                    response.trace_id,
                    observation,
                    sequence=sequence,
                    trace_attempt_number=trace.attempt_count,
                    parent_observation_id=parent_observation_id,
                    expires_at=trace_expires_at,
                )
                sequence += 1

            for index, call in enumerate(tool_calls):
                result = tool_results[index] if index < len(tool_results) else {}
                self._add_tool_trace(
                    session,
                    request_id,
                    patient_id_hash,
                    response.trace_id,
                    call,
                    result,
                    sequence=sequence,
                    trace_attempt_number=trace.attempt_count,
                    parent_observation_id=parent_observation_id,
                    now=now,
                    trace_expires_at=trace_expires_at,
                    tool_expires_at=tool_expires_at,
                )
                sequence += 1

            final_observation_id = _observation_id()
            final_evidence = self._encrypted_evidence_fields(
                trace_id=response.trace_id,
                observation_id=final_observation_id,
                step_type="final_response",
                sequence=sequence,
                trace_attempt_number=trace.attempt_count,
                value={
                    "evidence_version": "v1",
                    "event_type": "final_response",
                    "agent_name": response.agent_name,
                    "decision_type": response.decision_type,
                    "route": trace.route,
                    "human_summary": response.human_summary,
                    "tool_calls": tool_calls,
                    "tool_results": tool_results,
                    "selection_state_resolution": (
                        structured.get(
                            "selection_state_resolution"
                        )
                        if isinstance(
                            structured.get(
                                "selection_state_resolution"
                            ),
                            dict,
                        )
                        else None
                    ),
                    "validation_passed": (
                        response.validation_passed
                    ),
                    "validation_errors": (
                        response.validation_errors
                    ),
                },
            )
            session.add(
                AgentRunStep(
                    trace_id=response.trace_id,
                    sequence=sequence,
                    observation_id=final_observation_id,
                    parent_observation_id=parent_observation_id,
                    observation_type="span",
                    trace_attempt_number=trace.attempt_count,
                    step_type="final_response",
                    step_name=response.decision_type,
                    status="COMPLETED",
                    latency_ms=trace.latency_ms,
                    input_tokens=trace.input_tokens,
                    output_tokens=trace.output_tokens,
                    estimated_cost_usd=trace.estimated_cost_usd,
                    output_hash=trace.output_hash,
                    prompt_version_id=response.prompt_version_id,
                    provider=trace.provider,
                    model_id=trace.model_id,
                    started_at=attempt_started_at,
                    completed_at=now,
                    created_at=now,
                    expires_at=trace_expires_at,
                    metadata_json=dump_json(
                        {
                            "response_hash": trace.output_hash,
                            "decision_evidence_encrypted": True,
                            "validation_passed": response.validation_passed,
                        }
                    ),
                    **final_evidence,
                )
            )
            enqueue_trace_attempt(
                session,
                trace=trace,
                settings=self.settings,
                recorded_at=now,
            )
            session.commit()

    def record_model_calls(
        self,
        *,
        trace_id: str,
        observations: list[dict[str, Any]],
    ) -> None:
        """Persist failed/incomplete generations before the final error step."""

        if not observations:
            return
        now = utc_now()
        expires_at = agent_observability_expires_at(now)
        with self.session_factory() as session:
            trace = session.scalar(
                select(AgentRunTrace)
                .where(AgentRunTrace.trace_id == trace_id)
                .with_for_update()
            )
            if trace is None:
                return
            sequence = _next_sequence(session, trace_id)
            parent_observation_id = _attempt_root_observation_id(
                session,
                trace_id,
                trace.attempt_count,
            )
            for observation in observations:
                self._add_model_trace(
                    session,
                    trace_id,
                    observation,
                    sequence=sequence,
                    trace_attempt_number=trace.attempt_count,
                    parent_observation_id=parent_observation_id,
                    expires_at=expires_at,
                )
                sequence += 1
            session.commit()

    def fail_chat(
        self,
        *,
        trace_id: str,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        now = utc_now()
        expires_at = agent_observability_expires_at(now)
        with self.session_factory() as session:
            trace = session.scalar(
                select(AgentRunTrace)
                .where(AgentRunTrace.trace_id == trace_id)
                .with_for_update()
            )
            if trace is None:
                return
            trace.status = "RETRYABLE_FAILED" if retryable else "FINAL_FAILED"
            trace.error_code = error_code
            trace.error_message = (
                redacted_clinical_text_label(
                    error_message,
                    key="error_message",
                )
                if error_message
                else ""
            )
            trace.completed_at = now
            trace.updated_at = now
            trace.expires_at = expires_at
            next_sequence = _next_sequence(session, trace_id)
            parent_observation_id = _attempt_root_observation_id(
                session,
                trace_id,
                trace.attempt_count,
            )
            attempt_started_at = _attempt_started_at(
                session,
                trace_id,
                trace.attempt_count,
            )
            trace.latency_ms = _duration_ms(attempt_started_at, now)
            final_observation_id = _observation_id()
            final_evidence = self._encrypted_evidence_fields(
                trace_id=trace_id,
                observation_id=final_observation_id,
                step_type="final_response",
                sequence=next_sequence,
                trace_attempt_number=trace.attempt_count,
                value={
                    "evidence_version": "v1",
                    "event_type": "error_response",
                    "error_code": error_code,
                    "error_message": error_message,
                    "retryable": retryable,
                },
            )
            session.add(
                AgentRunStep(
                    trace_id=trace_id,
                    sequence=next_sequence,
                    observation_id=final_observation_id,
                    parent_observation_id=parent_observation_id,
                    observation_type="span",
                    trace_attempt_number=trace.attempt_count,
                    step_type="final_response",
                    step_name="error",
                    status=trace.status,
                    error_code=error_code,
                    level="ERROR",
                    status_message=trace.error_message,
                    latency_ms=trace.latency_ms,
                    started_at=attempt_started_at,
                    completed_at=now,
                    created_at=now,
                    expires_at=expires_at,
                    metadata_json=dump_json(
                        {
                            "retryable": retryable,
                            "decision_evidence_encrypted": True,
                        }
                    ),
                    **final_evidence,
                )
            )
            enqueue_trace_attempt(
                session,
                trace=trace,
                settings=self.settings,
                recorded_at=now,
            )
            session.commit()

    def _add_model_trace(
        self,
        session: Session,
        trace_id: str,
        observation: dict[str, Any],
        *,
        sequence: int,
        trace_attempt_number: int,
        parent_observation_id: str,
        expires_at: datetime,
    ) -> None:
        observation_id = str(
            observation.get("observation_id")
            or _observation_id()
        )
        if session.scalar(
            select(AgentRunStep.id).where(
                AgentRunStep.trace_id == trace_id,
                AgentRunStep.observation_id == observation_id,
            )
        ) is not None:
            return
        usage = (
            observation.get("usage_details")
            if isinstance(observation.get("usage_details"), dict)
            else {}
        )
        input_tokens = _nonnegative_int(
            usage.get("input") or usage.get("input_tokens")
        )
        output_tokens = _nonnegative_int(
            usage.get("output") or usage.get("output_tokens")
        )
        estimated_cost = estimate_model_cost_usd(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_usd_per_1m_tokens=(
                self.settings.agent_cost_input_usd_per_1m_tokens
            ),
            output_usd_per_1m_tokens=(
                self.settings.agent_cost_output_usd_per_1m_tokens
            ),
        )
        cost_details = (
            {"total": estimated_cost}
            if estimated_cost
            else {}
        )
        decision_evidence = (
            observation.get("decision_evidence")
            if isinstance(
                observation.get("decision_evidence"),
                dict,
            )
            else {}
        )
        evidence = self._encrypted_evidence_fields(
            trace_id=trace_id,
            observation_id=observation_id,
            step_type="model_call",
            sequence=sequence,
            trace_attempt_number=trace_attempt_number,
            value=decision_evidence,
        )
        reason_code = str(
            decision_evidence.get("system_reason_code") or ""
        )
        session.add(
            AgentRunStep(
                trace_id=trace_id,
                sequence=sequence,
                observation_id=observation_id,
                parent_observation_id=parent_observation_id,
                observation_type="generation",
                trace_attempt_number=trace_attempt_number,
                step_type="model_call",
                step_name=str(observation.get("name") or "model_call"),
                status=str(
                    observation.get("status") or "COMPLETED"
                ),
                latency_ms=_nonnegative_int(
                    observation.get("latency_ms")
                ),
                time_to_first_token_ms=_nonnegative_int(
                    observation.get("time_to_first_token_ms")
                ),
                error_code=str(observation.get("error_code") or "")[:80],
                level=str(observation.get("level") or "DEFAULT")[:20],
                status_message=(
                    redacted_clinical_text_label(
                        observation.get("status_message"),
                        key="status_message",
                    )
                    if observation.get("status_message")
                    else ""
                ),
                prompt_version_id=str(
                    observation.get("prompt_version_id") or ""
                )[:120],
                provider=str(observation.get("provider") or "")[:80],
                model_id=str(observation.get("model_id") or "")[:255],
                input_hash=str(observation.get("input_hash") or "")[:64],
                output_hash=str(observation.get("output_hash") or "")[:64],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=estimated_cost,
                model_parameters_json=dump_json(
                    _json_value(
                        observation.get("model_parameters"),
                        default={},
                    )
                ),
                usage_details_json=dump_json(usage),
                cost_details_json=dump_json(cost_details),
                started_at=_parse_datetime(
                    observation.get("started_at")
                ),
                completion_start_time=_parse_datetime(
                    observation.get("completion_start_time")
                ),
                completed_at=_parse_datetime(
                    observation.get("completed_at")
                ),
                created_at=utc_now(),
                expires_at=expires_at,
                metadata_json=dump_json(
                    {
                        "content_retained": True,
                        "content_storage": "encrypted_agent_db_only",
                        "decision_evidence_encrypted": True,
                        "decision_reason_code": reason_code,
                        "private_reasoning_retained": False,
                        "langfuse_observation_type": "generation",
                    }
                ),
                **evidence,
            )
        )

    def _add_tool_trace(
        self,
        session: Session,
        request_id: str,
        patient_id_hash: str,
        trace_id: str,
        call: dict[str, Any],
        result: dict[str, Any],
        *,
        sequence: int,
        trace_attempt_number: int,
        parent_observation_id: str,
        now,
        trace_expires_at,
        tool_expires_at,
    ) -> None:
        tool_name = str(
            call.get("name") or call.get("tool_name") or "unknown"
        )
        tool_call_id = str(call.get("id") or call.get("tool_call_id") or f"{sequence}:{tool_name}")
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else call.get("args")
        arguments = arguments if isinstance(arguments, dict) else {}
        response_payload = result.get("response") if isinstance(result.get("response"), dict) else {}
        metadata = MODEL_VISIBLE_TOOL_METADATA.get(tool_name, {})
        result_status = str(result.get("status") or "unknown").upper()
        error_code = _safe_error_code(result.get("error"))
        side_effect_level = _side_effect_level(metadata)
        attempt_count = max(
            1,
            _nonnegative_int(result.get("attempt_count")),
        )
        latency_ms = _nonnegative_int(
            result.get("elapsed_ms")
            or response_payload.get("elapsed_ms")
        )
        retryable = bool(result.get("retryable"))
        started_at = now - timedelta(milliseconds=latency_ms)

        session.add(
            AgentToolExecution(
                trace_id=trace_id,
                request_id=request_id,
                patient_id_hash=patient_id_hash,
                tool_call_id=tool_call_id,
                trace_attempt_number=trace_attempt_number,
                tool_name=tool_name,
                tool_version="",
                argument_schema_version="v1",
                argument_hash=_sha256_json(arguments),
                status=result_status,
                side_effect_level=side_effect_level,
                attempt_count=attempt_count,
                latency_ms=latency_ms,
                response_hash=_sha256_json(response_payload),
                error_code=error_code,
                retryable=retryable,
                metadata_json=dump_json(
                    {
                        "mutability": metadata.get("mutability", ""),
                        "risk_level": metadata.get("risk_level", ""),
                        "confirmation_policy": metadata.get("confirmation_policy", ""),
                    }
                ),
                started_at=started_at,
                completed_at=now,
                created_at=now,
                updated_at=now,
                expires_at=tool_expires_at,
            )
        )
        tool_observation_id = _observation_id()
        tool_evidence = self._encrypted_evidence_fields(
            trace_id=trace_id,
            observation_id=tool_observation_id,
            step_type="tool_call",
            sequence=sequence,
            trace_attempt_number=trace_attempt_number,
            value={
                "evidence_version": "v1",
                "event_type": "tool_call",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "status": result_status,
                "response": response_payload,
                "error": str(result.get("error") or ""),
                "attempt_count": attempt_count,
                "retryable": retryable,
            },
        )
        session.add(
            AgentRunStep(
                trace_id=trace_id,
                sequence=sequence,
                observation_id=tool_observation_id,
                parent_observation_id=parent_observation_id,
                observation_type="tool",
                trace_attempt_number=trace_attempt_number,
                step_type="tool_call",
                step_name=tool_name,
                status=result_status,
                tool_name=tool_name,
                argument_schema_version="v1",
                argument_hash=_sha256_json(arguments),
                side_effect_level=side_effect_level,
                retry_count=max(0, attempt_count - 1),
                latency_ms=latency_ms,
                error_code=error_code,
                level="ERROR" if result_status == "ERROR" else "DEFAULT",
                status_message=(
                    redacted_clinical_text_label(
                        result.get("error"),
                        key="tool_error",
                    )
                    if result.get("error")
                    else ""
                ),
                input_hash=_sha256_json(arguments),
                output_hash=_sha256_json(response_payload),
                started_at=started_at,
                completed_at=now,
                created_at=now,
                expires_at=trace_expires_at,
                metadata_json=dump_json(
                    {
                        "response_hash": _sha256_json(response_payload),
                        "decision_evidence_encrypted": True,
                        "risk_level": metadata.get("risk_level", ""),
                        "retryable": retryable,
                        "langfuse_observation_type": "tool",
                    }
                ),
                **tool_evidence,
            )
        )

    def _encrypted_evidence_fields(
        self,
        *,
        trace_id: str,
        observation_id: str,
        step_type: str,
        sequence: int,
        trace_attempt_number: int,
        value: Any,
    ) -> dict[str, str]:
        ciphertext, evidence_hash = (
            self.evidence_cipher.encrypt_json(
                value,
                context=TraceEvidenceContext(
                    trace_id=trace_id,
                    observation_id=observation_id,
                    step_type=step_type,
                    sequence=sequence,
                    trace_attempt_number=trace_attempt_number,
                ),
            )
        )
        return {
            "evidence_ciphertext": ciphertext,
            "evidence_hash": evidence_hash,
            "encryption_key_id": self.evidence_cipher.key_id,
        }


def _token_usage(structured: dict[str, Any]) -> dict[str, int]:
    observations = _dict_list(
        structured.get("model_call_observations")
    )
    if observations:
        return {
            "input_tokens": sum(
                _nonnegative_int(
                    (
                        observation.get("usage_details")
                        if isinstance(
                            observation.get("usage_details"),
                            dict,
                        )
                        else {}
                    ).get("input")
                )
                for observation in observations
            ),
            "output_tokens": sum(
                _nonnegative_int(
                    (
                        observation.get("usage_details")
                        if isinstance(
                            observation.get("usage_details"),
                            dict,
                        )
                        else {}
                    ).get("output")
                )
                for observation in observations
            ),
        }

    root_usage = (
        structured.get("token_usage")
        if isinstance(structured.get("token_usage"), dict)
        else {}
    )
    if root_usage:
        return {
            "input_tokens": _nonnegative_int(
                root_usage.get("input_tokens")
            ),
            "output_tokens": _nonnegative_int(
                root_usage.get("output_tokens")
            ),
        }

    candidates: list[dict[str, Any]] = []
    for key in (
        "model_output",
        "final_model_output",
        "supervisor_model_output",
        "supervisor_final_model_output",
    ):
        value = structured.get(key)
        if isinstance(value, dict):
            candidates.append(value)

    input_tokens = 0
    output_tokens = 0
    seen: set[int] = set()
    for candidate in candidates:
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        raw = candidate.get("token_usage")
        usage = raw if isinstance(raw, dict) else {}
        input_tokens += _nonnegative_int(
            usage.get("input_tokens")
            or candidate.get("input_tokens")
        )
        output_tokens += _nonnegative_int(
            usage.get("output_tokens")
            or candidate.get("output_tokens")
        )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _next_sequence(session: Session, trace_id: str) -> int:
    return (
        session.scalar(
            select(AgentRunStep.sequence)
            .where(AgentRunStep.trace_id == trace_id)
            .order_by(AgentRunStep.sequence.desc())
            .limit(1)
        )
        or 0
    ) + 1


def _attempt_root_observation_id(
    session: Session,
    trace_id: str,
    trace_attempt_number: int,
) -> str:
    return str(
        session.scalar(
            select(AgentRunStep.observation_id)
            .where(
                AgentRunStep.trace_id == trace_id,
                AgentRunStep.trace_attempt_number
                == trace_attempt_number,
                AgentRunStep.step_type == "request_ingress",
            )
            .order_by(AgentRunStep.sequence.desc())
            .limit(1)
        )
        or ""
    )


def _attempt_started_at(
    session: Session,
    trace_id: str,
    trace_attempt_number: int,
) -> datetime | None:
    return session.scalar(
        select(AgentRunStep.started_at)
        .where(
            AgentRunStep.trace_id == trace_id,
            AgentRunStep.trace_attempt_number
            == trace_attempt_number,
            AgentRunStep.step_type == "request_ingress",
        )
        .order_by(AgentRunStep.sequence.desc())
        .limit(1)
    )


def _duration_ms(
    started_at: datetime | None,
    completed_at: datetime,
) -> int:
    if started_at is None:
        return 0
    return max(
        0,
        round((completed_at - started_at).total_seconds() * 1000),
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except ValueError:
        return None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_value(value: Any, *, default: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)):
        return value
    return default


def _observation_id() -> str:
    return uuid.uuid4().hex


def _safe_error_code(value: Any) -> str:
    text = str(value or "").strip()
    if (
        text
        and len(text) <= 80
        and all(
            character.isascii()
            and (
                character.isalnum()
                or character in "_:-."
            )
            for character in text
        )
    ):
        return text
    return "TOOL_EXECUTION_ERROR" if text else ""


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _side_effect_level(metadata: dict[str, str]) -> str:
    mutability = metadata.get("mutability", "")
    if mutability in {"write", "delete"}:
        return "approval_required"
    if mutability == "propose":
        return "deferred"
    return "read_only"
