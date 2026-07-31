from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent_app.observability.outbox import (
    SCORE,
    TRACE_ATTEMPT,
    TRACE_DELETE,
    ObservabilityExportWorkItem,
    deterministic_langfuse_trace_id,
    deterministic_span_id,
)
from agent_app.persistence.models import AgentRunStep, AgentRunTrace
from shared.json_utils import parse_json_object
from shared.settings import Settings


class LangfuseExportError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LangfuseHttpExporter:
    """Acknowledged OTLP/HTTP exporter for the durable Agent outbox."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        client: httpx.Client | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        base_url, public_key, secret_key = (
            settings.require_langfuse_export_config()
        )
        self.base_url = base_url
        self._owns_client = client is None
        self.client = client or httpx.Client(
            auth=httpx.BasicAuth(public_key, secret_key),
            timeout=settings.langfuse_request_timeout_seconds,
            verify=settings.langfuse_verify_tls,
            headers={"User-Agent": "dranswer-agent-langfuse-exporter/1"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def export(self, item: ObservabilityExportWorkItem) -> None:
        if item.event_type == TRACE_ATTEMPT:
            self._export_trace_attempt(item)
            return
        if item.event_type == SCORE:
            self._export_score(item)
            return
        if item.event_type == TRACE_DELETE:
            self._delete_trace(item)
            return
        raise LangfuseExportError(
            "UNSUPPORTED_EVENT_TYPE",
            f"unsupported observability event type: {item.event_type}",
            retryable=False,
        )

    def _export_trace_attempt(
        self,
        item: ObservabilityExportWorkItem,
    ) -> None:
        with self.session_factory() as session:
            trace = session.scalar(
                select(AgentRunTrace).where(
                    AgentRunTrace.trace_id == item.trace_id
                )
            )
            steps = list(
                session.scalars(
                    select(AgentRunStep)
                    .where(
                        AgentRunStep.trace_id == item.trace_id,
                        AgentRunStep.trace_attempt_number
                        == item.trace_attempt_number,
                    )
                    .order_by(AgentRunStep.sequence)
                ).all()
            )
        if trace is None:
            raise LangfuseExportError(
                "TRACE_SOURCE_MISSING",
                "trace source was removed before export",
                retryable=False,
            )
        if not steps:
            raise LangfuseExportError(
                "TRACE_STEPS_MISSING",
                "trace attempt has no observations",
                retryable=False,
            )
        payload = build_otlp_trace_payload(
            trace,
            steps,
            trace_attempt_number=item.trace_attempt_number,
        )
        self._request(
            "POST",
            f"{self.base_url}/api/public/otel/v1/traces",
            json_payload=payload,
            headers={
                "Content-Type": "application/json",
                "x-langfuse-ingestion-version": "4",
            },
            reject_partial_otlp=True,
        )

    def _export_score(self, item: ObservabilityExportWorkItem) -> None:
        required = {"id", "name", "value", "dataType"}
        if not required <= set(item.payload):
            raise LangfuseExportError(
                "SCORE_PAYLOAD_INVALID",
                "score payload is missing required fields",
                retryable=False,
            )
        self._request(
            "POST",
            f"{self.base_url}/api/public/scores",
            json_payload=item.payload,
            headers={"Content-Type": "application/json"},
        )

    def _delete_trace(self, item: ObservabilityExportWorkItem) -> None:
        remote_trace_id = str(
            item.payload.get("traceId")
            or deterministic_langfuse_trace_id(item.trace_id)
        )
        self._request(
            "DELETE",
            f"{self.base_url}/api/public/traces/{remote_trace_id}",
            allow_not_found=True,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
        reject_partial_otlp: bool = False,
    ) -> httpx.Response:
        try:
            response = self.client.request(
                method,
                url,
                json=json_payload,
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise LangfuseExportError(
                "LANGFUSE_NETWORK_ERROR",
                exc.__class__.__name__,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise LangfuseExportError(
                "LANGFUSE_HTTP_CLIENT_ERROR",
                exc.__class__.__name__,
                retryable=True,
            ) from exc

        if 200 <= response.status_code < 300:
            if reject_partial_otlp:
                self._raise_for_partial_otlp(response)
            return response
        if allow_not_found and response.status_code == 404:
            return response
        retryable = (
            response.status_code in {408, 425, 429}
            or response.status_code >= 500
        )
        raise LangfuseExportError(
            f"LANGFUSE_HTTP_{response.status_code}",
            f"Langfuse returned HTTP {response.status_code}",
            retryable=retryable,
        )

    @staticmethod
    def _raise_for_partial_otlp(response: httpx.Response) -> None:
        if not response.content:
            return
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            return
        if not isinstance(body, dict):
            return
        partial = body.get("partialSuccess") or body.get(
            "partial_success"
        )
        if not isinstance(partial, dict):
            return
        rejected = partial.get("rejectedSpans")
        if rejected is None:
            rejected = partial.get("rejected_spans")
        try:
            rejected_count = int(rejected or 0)
        except (TypeError, ValueError):
            rejected_count = 0
        if rejected_count > 0:
            raise LangfuseExportError(
                "LANGFUSE_OTLP_PARTIAL_REJECT",
                f"Langfuse rejected {rejected_count} OTLP spans",
                retryable=False,
            )


def build_otlp_trace_payload(
    trace: AgentRunTrace,
    steps: list[AgentRunStep],
    *,
    trace_attempt_number: int,
) -> dict[str, Any]:
    """Build one complete, PHI-free OTLP batch for a single retry attempt."""

    ordered_steps = sorted(steps, key=lambda row: row.sequence)
    root = next(
        (
            step
            for step in ordered_steps
            if step.step_type == "request_ingress"
        ),
        ordered_steps[0],
    )
    span_ids = {
        step.observation_id: deterministic_span_id(
            step.observation_id or f"{trace.trace_id}:{step.sequence}"
        )
        for step in ordered_steps
    }
    root_span_id = span_ids[root.observation_id]
    trace_hex = deterministic_langfuse_trace_id(trace.trace_id)
    attempt_start = min(
        (
            step.started_at or step.created_at
            for step in ordered_steps
        ),
        default=trace.started_at,
    )
    attempt_end = max(
        (
            step.completed_at
            or step.started_at
            or step.created_at
            for step in ordered_steps
        ),
        default=trace.completed_at or trace.updated_at,
    )
    final_step = next(
        (
            step
            for step in reversed(ordered_steps)
            if step.step_type == "final_response"
        ),
        ordered_steps[-1],
    )
    trace_attributes = _trace_attributes(
        trace,
        trace_attempt_number=trace_attempt_number,
        attempt_status=final_step.status,
    )
    spans: list[dict[str, Any]] = []
    for step in ordered_steps:
        is_root = step is root
        start = attempt_start if is_root else (
            step.started_at or step.created_at
        )
        end = attempt_end if is_root else (
            step.completed_at or start
        )
        if end < start:
            end = start
        parent_span_id = ""
        if not is_root:
            parent_span_id = span_ids.get(
                step.parent_observation_id,
                root_span_id,
            )
        attributes = [
            *trace_attributes,
            *_observation_attributes(
                trace,
                step,
                is_root=is_root,
                trace_attempt_number=trace_attempt_number,
                root_input_hash=trace.input_hash,
                root_output_hash=final_step.output_hash,
            ),
        ]
        status_is_error = (
            step.level.upper() == "ERROR"
            or bool(step.error_code)
            or step.status.upper()
            in {"FINAL_FAILED", "RETRYABLE_FAILED", "FAILED", "ERROR"}
        )
        span = {
            "traceId": trace_hex,
            "spanId": span_ids[step.observation_id],
            "name": (
                f"{trace.workflow_name}.attempt.{trace_attempt_number}"
                if is_root
                else (step.step_name or step.step_type)
            ),
            "kind": 1,
            "startTimeUnixNano": str(_unix_nano(start)),
            "endTimeUnixNano": str(max(_unix_nano(end), _unix_nano(start) + 1)),
            "attributes": attributes,
            "status": {
                "code": 2 if status_is_error else 1,
                "message": (
                    step.status_message or step.error_code
                    if status_is_error
                    else ""
                )[:500],
            },
            "flags": 1,
        }
        if parent_span_id:
            span["parentSpanId"] = parent_span_id
        spans.append(span)

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attribute("service.name", "dranswer-agent"),
                        _attribute(
                            "service.version",
                            trace.release_version or "unknown",
                        ),
                        _attribute(
                            "deployment.environment.name",
                            trace.environment or "unknown",
                        ),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "dranswer.agent.langfuse_exporter",
                            "version": "1",
                        },
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def _trace_attributes(
    trace: AgentRunTrace,
    *,
    trace_attempt_number: int,
    attempt_status: str,
) -> list[dict[str, Any]]:
    metadata = parse_json_object(trace.metadata_json)
    tags = metadata.get("langfuse_tags")
    safe_tags = [
        str(value)[:200]
        for value in (tags if isinstance(tags, list) else [])
        if str(value).strip()
    ]
    if not safe_tags:
        safe_tags = [
            value
            for value in (
                trace.workflow_name,
                trace.agent_name,
                trace.decision_type,
            )
            if value
        ]
    attributes = [
        _attribute("langfuse.trace.name", trace.workflow_name),
        _attribute("langfuse.user.id", trace.patient_id_hash),
        _attribute("langfuse.session.id", trace.patient_id_hash),
        _attribute("langfuse.trace.tags", safe_tags),
        _attribute("langfuse.environment", trace.environment),
        _attribute("langfuse.release", trace.release_version),
        _attribute(
            "langfuse.trace.metadata.workflow",
            trace.workflow_name,
        ),
        _attribute(
            "langfuse.trace.metadata.requestidhash",
            _sha256_text(trace.request_id),
        ),
        _attribute(
            "langfuse.trace.metadata.apipath",
            trace.api_path,
        ),
        _attribute(
            "langfuse.trace.metadata.attempt",
            trace_attempt_number,
        ),
        _attribute(
            "langfuse.trace.metadata.attemptstatus",
            attempt_status,
        ),
    ]
    return [attribute for attribute in attributes if attribute is not None]


def _observation_attributes(
    trace: AgentRunTrace,
    step: AgentRunStep,
    *,
    is_root: bool,
    trace_attempt_number: int,
    root_input_hash: str,
    root_output_hash: str,
) -> list[dict[str, Any]]:
    observation_type = (
        step.observation_type
        if step.observation_type in {"span", "generation", "event"}
        else "span"
    )
    input_hash = root_input_hash if is_root else step.input_hash
    output_hash = root_output_hash if is_root else step.output_hash
    attributes: list[dict[str, Any] | None] = [
        _attribute("langfuse.observation.type", observation_type),
        _attribute("langfuse.observation.level", step.level or "DEFAULT"),
        _attribute(
            "langfuse.observation.status_message",
            step.status_message[:500],
        ),
        _attribute(
            "langfuse.observation.input",
            _hash_envelope(input_hash),
        ),
        _attribute(
            "langfuse.observation.output",
            _hash_envelope(output_hash),
        ),
        _attribute(
            "langfuse.observation.metadata.agentobservationid",
            _sha256_text(step.observation_id)
            if step.observation_id
            else "",
        ),
        _attribute(
            "langfuse.observation.metadata.steptype",
            step.step_type,
        ),
        _attribute(
            "langfuse.observation.metadata.status",
            step.status,
        ),
        _attribute(
            "langfuse.observation.metadata.attempt",
            trace_attempt_number,
        ),
        _attribute(
            "langfuse.observation.metadata.retrycount",
            step.retry_count,
        ),
        _attribute(
            "langfuse.observation.metadata.latencyms",
            step.latency_ms,
        ),
        _attribute(
            "langfuse.observation.metadata.errorcode",
            step.error_code,
        ),
        _attribute(
            "langfuse.observation.metadata.toolname",
            step.tool_name,
        ),
        _attribute(
            "langfuse.observation.metadata.sideeffectlevel",
            step.side_effect_level,
        ),
        _attribute(
            "langfuse.observation.metadata.promptversionid",
            step.prompt_version_id,
        ),
    ]
    if observation_type in {"generation", "embedding"}:
        usage = parse_json_object(step.usage_details_json)
        if not usage and (step.input_tokens or step.output_tokens):
            usage = {
                "input": step.input_tokens,
                "output": step.output_tokens,
                "total": step.input_tokens + step.output_tokens,
            }
        cost = parse_json_object(step.cost_details_json)
        if not cost and step.estimated_cost_usd:
            cost = {"total": step.estimated_cost_usd}
        attributes.extend(
            [
                _attribute(
                    "langfuse.observation.model.name",
                    step.model_id or trace.model_id,
                ),
                _attribute(
                    "langfuse.observation.model.parameters",
                    _json_string(
                        parse_json_object(step.model_parameters_json)
                    ),
                ),
                _attribute(
                    "langfuse.observation.usage_details",
                    _json_string(usage),
                ),
                _attribute(
                    "langfuse.observation.cost_details",
                    _json_string(cost),
                ),
                _attribute(
                    "langfuse.observation.completion_start_time",
                    (
                        _isoformat(step.completion_start_time)
                        if step.completion_start_time is not None
                        else ""
                    ),
                ),
                _attribute(
                    "langfuse.observation.metadata.provider",
                    step.provider or trace.provider,
                ),
            ]
        )
    return [attribute for attribute in attributes if attribute is not None]


def _attribute(key: str, value: Any) -> dict[str, Any] | None:
    if value is None or value == "" or value == [] or value == {}:
        return None
    return {
        "key": key,
        "value": _any_value(value),
    }


def _any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {
            "arrayValue": {
                "values": [_any_value(item) for item in value]
            }
        }
    return {"stringValue": str(value)}


def _hash_envelope(value: str) -> str:
    if not value:
        return ""
    return _json_string(
        {
            "sha256": value,
            "redacted": True,
        }
    )


def _json_string(value: Any) -> str:
    if value in ({}, [], None, ""):
        return ""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _unix_nano(value: datetime) -> int:
    aware = (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
    return int(aware.timestamp() * 1_000_000_000)


def _isoformat(value: datetime) -> str:
    aware = (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
    return aware.isoformat()


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
