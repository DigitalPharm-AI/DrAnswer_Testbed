from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.chat_contracts import ChatSyncRequest
from agent_app.persistence.models import AgentRunStep, AgentRunTrace, AgentToolExecution
from agent_app.tools.names import MODEL_VISIBLE_TOOL_METADATA, canonical_tool_name
from shared.json_utils import dump_json
from shared.readiness_budget import estimate_model_cost_usd
from shared.redaction import redact_inline_secrets
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

    def start_chat(self, request: ChatSyncRequest, *, trace_id: str, api_path: str) -> None:
        now = utc_now()
        expires_at = now + timedelta(seconds=max(1, self.settings.agent_trace_retention_seconds))
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
                    conversation_id=request.conversation_id,
                    message_id=request.message_id,
                    patient_id_hash=_sha256_text(request.patient_id),
                    environment=self.settings.app_env,
                    release_version=self.settings.app_release_version,
                    workflow_name="multiturn_chat",
                    status="PROCESSING",
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
                # when SQLite/PostgreSQL foreign keys are enforced.
                session.flush()
            else:
                trace.status = "PROCESSING"
                trace.updated_at = now
                trace.expires_at = expires_at

            session.execute(
                delete(AgentRunStep).where(AgentRunStep.trace_id == trace_id)
            )
            session.add(
                AgentRunStep(
                    trace_id=trace_id,
                    sequence=1,
                    step_type="request_ingress",
                    step_name="sync_chat",
                    status="COMPLETED",
                    started_at=now,
                    completed_at=now,
                    created_at=now,
                    expires_at=expires_at,
                    metadata_json=dump_json({"api_path": api_path}),
                )
            )
            session.commit()

    def complete_chat(
        self,
        request: ChatSyncRequest,
        response: AgentResponse,
    ) -> None:
        now = utc_now()
        trace_expires_at = now + timedelta(seconds=max(1, self.settings.agent_trace_retention_seconds))
        tool_expires_at = now + timedelta(seconds=max(1, self.settings.agent_tool_execution_retention_seconds))
        structured = response.structured_payload if isinstance(response.structured_payload, dict) else {}
        token_usage = _token_usage(structured)
        tool_calls = _dict_list(structured.get("tool_calls"))
        tool_results = _dict_list(structured.get("tool_results"))

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
            trace.fallback_reason = str(structured.get("final_answer_source") or "")
            trace.prompt_version_id = response.prompt_version_id
            trace.provider = str(structured.get("provider") or self.settings.llm_provider)
            trace.model_tier = str(structured.get("model_tier") or self.settings.llm_model_tier)
            trace.model_id = str(
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
            trace.latency_ms = _nonnegative_int(
                structured.get("elapsed_ms") or structured.get("latency_ms")
            )
            trace.tool_count = len(tool_calls)
            trace.error_code = ""
            trace.error_message = ""
            trace.metadata_json = dump_json(
                {
                    "validation_passed": response.validation_passed,
                    "validation_error_count": len(response.validation_errors),
                    "structured_payload_keys": sorted(str(key) for key in structured),
                    "requires_conversation_alert": response.requires_conversation_alert,
                }
            )
            trace.completed_at = now
            trace.updated_at = now
            trace.expires_at = trace_expires_at

            session.execute(
                delete(AgentRunStep).where(
                    AgentRunStep.trace_id == response.trace_id,
                    AgentRunStep.sequence > 1,
                )
            )
            session.execute(
                delete(AgentToolExecution).where(
                    AgentToolExecution.trace_id == response.trace_id
                )
            )
            for index, call in enumerate(tool_calls):
                result = tool_results[index] if index < len(tool_results) else {}
                self._add_tool_trace(
                    session,
                    request,
                    response.trace_id,
                    call,
                    result,
                    sequence=index + 2,
                    now=now,
                    trace_expires_at=trace_expires_at,
                    tool_expires_at=tool_expires_at,
                )

            final_sequence = len(tool_calls) + 2
            session.add(
                AgentRunStep(
                    trace_id=response.trace_id,
                    sequence=final_sequence,
                    step_type="final_response",
                    step_name=response.decision_type,
                    status="COMPLETED",
                    latency_ms=trace.latency_ms,
                    started_at=trace.started_at,
                    completed_at=now,
                    created_at=now,
                    expires_at=trace_expires_at,
                    metadata_json=dump_json(
                        {
                            "response_hash": trace.output_hash,
                            "validation_passed": response.validation_passed,
                        }
                    ),
                )
            )
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
        expires_at = now + timedelta(seconds=max(1, self.settings.agent_trace_retention_seconds))
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
            trace.error_message = redact_inline_secrets(error_message, limit=500)
            trace.completed_at = now
            trace.updated_at = now
            trace.expires_at = expires_at
            next_sequence = (
                session.scalar(
                    select(AgentRunStep.sequence)
                    .where(AgentRunStep.trace_id == trace_id)
                    .order_by(AgentRunStep.sequence.desc())
                    .limit(1)
                )
                or 0
            ) + 1
            session.add(
                AgentRunStep(
                    trace_id=trace_id,
                    sequence=next_sequence,
                    step_type="final_response",
                    step_name="error",
                    status=trace.status,
                    error_code=error_code,
                    started_at=trace.started_at,
                    completed_at=now,
                    created_at=now,
                    expires_at=expires_at,
                    metadata_json=dump_json({"retryable": retryable}),
                )
            )
            session.commit()

    @staticmethod
    def _add_tool_trace(
        session: Session,
        request: ChatSyncRequest,
        trace_id: str,
        call: dict[str, Any],
        result: dict[str, Any],
        *,
        sequence: int,
        now,
        trace_expires_at,
        tool_expires_at,
    ) -> None:
        tool_name = canonical_tool_name(str(call.get("name") or call.get("tool_name") or "unknown"))
        tool_call_id = str(call.get("id") or call.get("tool_call_id") or f"{sequence}:{tool_name}")
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else call.get("args")
        arguments = arguments if isinstance(arguments, dict) else {}
        response_payload = result.get("response") if isinstance(result.get("response"), dict) else {}
        metadata = MODEL_VISIBLE_TOOL_METADATA.get(tool_name, {})
        result_status = str(result.get("status") or "unknown").upper()
        error_code = str(result.get("error") or "")
        side_effect_level = _side_effect_level(metadata)

        session.add(
            AgentToolExecution(
                trace_id=trace_id,
                request_id=request.request_id,
                conversation_id=request.conversation_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_version="",
                argument_schema_version="v1",
                argument_hash=_sha256_json(arguments),
                status=result_status,
                side_effect_level=side_effect_level,
                attempt_count=1,
                response_hash=_sha256_json(response_payload),
                error_code=error_code,
                retryable=False,
                metadata_json=dump_json(
                    {
                        "mutability": metadata.get("mutability", ""),
                        "risk_level": metadata.get("risk_level", ""),
                        "confirmation_policy": metadata.get("confirmation_policy", ""),
                    }
                ),
                started_at=now,
                completed_at=now,
                created_at=now,
                updated_at=now,
                expires_at=tool_expires_at,
            )
        )
        session.add(
            AgentRunStep(
                trace_id=trace_id,
                sequence=sequence,
                step_type="tool_call",
                step_name=tool_name,
                status=result_status,
                tool_name=tool_name,
                argument_schema_version="v1",
                argument_hash=_sha256_json(arguments),
                side_effect_level=side_effect_level,
                retry_count=0,
                error_code=error_code,
                started_at=now,
                completed_at=now,
                created_at=now,
                expires_at=trace_expires_at,
                metadata_json=dump_json(
                    {
                        "response_hash": _sha256_json(response_payload),
                        "risk_level": metadata.get("risk_level", ""),
                    }
                ),
            )
        )


def _token_usage(structured: dict[str, Any]) -> dict[str, int]:
    raw = structured.get("token_usage")
    usage = raw if isinstance(raw, dict) else {}
    return {
        "input_tokens": _nonnegative_int(
            usage.get("input_tokens") or structured.get("input_tokens")
        ),
        "output_tokens": _nonnegative_int(
            usage.get("output_tokens") or structured.get("output_tokens")
        ),
    }


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
