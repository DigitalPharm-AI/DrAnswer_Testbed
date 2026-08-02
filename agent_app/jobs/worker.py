from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import uuid
from datetime import UTC, date, datetime
from math import ceil
from time import monotonic
from typing import Any

import httpx

from agent_app.errors import public_processing_error
from agent_app.jobs.daily_pattern_tasks import (
    DAILY_PATTERN_ANALYSIS_TASK,
    DAILY_PATTERN_DELIVERY_TASK,
    enqueue_proposal_delivery,
)
from agent_app.jobs.status import (
    mark_worker_started,
    mark_worker_stopped,
    record_worker_heartbeat,
    record_worker_task_completed,
)
from agent_app.jobs.tasks import (
    claim_next_async_task,
    mark_async_task_done,
    mark_async_task_failed,
    mark_async_task_terminal_failure,
    mark_callback_sent,
    stage_async_task_callback_delivery,
    task_callback_context,
    task_payload,
)
from agent_app.observability.model_calls import (
    capture_model_calls,
    response_with_model_calls,
)
from agent_app.observability.tool_calls import capture_tool_calls
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.persistence.db import SessionLocal
from agent_app.persistence.models import AgentAsyncTask
from agent_app.persistence.trace_store import AgentTraceStore
from agent_app.tools.backend_query import (
    BackendQueryTools,
    BackendReadContractError,
)
from shared.async_v13_contracts import (
    AsyncProcessingError,
    AsyncResultCallbackAck,
    MissedDoseEventRequest,
    MissedDoseResultCallback,
    NotificationPolicyChangeProposalRequest,
    NotificationPolicyProposalAck,
)
from shared.async_v13_contracts import (
    MissedDoseResult as MissedDoseCallbackResult,
)
from shared.chat_contracts import llm_safe_patient_snapshot
from shared.redaction import safe_exception_summary
from shared.schemas import (
    AgentAsyncClinicianAlertRequest,
    AgentAsyncPushMessageRequest,
    AgentCallbackContext,
    AgentResponse,
    MissedDoseEventPayload,
)
from shared.settings import get_settings

logger = logging.getLogger("uvicorn.error")
ASYNC_CALLBACK_TIMEOUT_SECONDS = 30
ASYNC_TASK_LEASE_BUFFER_SECONDS = 5
_ALLOWED_CALLBACK_PATHS = {
    "/api/agent/async/missed-dose-results",
    "/api/agent/async/notification-policy-change-proposals",
    "/api/agent/async/push-messages",
    "/api/agent/async/clinician-alerts",
}


class AgentGenerationTimeoutError(TimeoutError):
    """The model call timed out with an uncertain upstream completion state."""

    retryable = False


def async_task_worker(
    stop_event: threading.Event,
    orchestrator: AgentLangGraphNativeOrchestrator,
    backend_queries: BackendQueryTools | None = None,
) -> None:
    worker_id = f"{socket.gethostname()}:{threading.current_thread().name}:{uuid.uuid4().hex[:8]}"
    heartbeat_interval = max(1, get_settings().agent_worker_heartbeat_interval_seconds)
    last_heartbeat = 0.0
    _record_worker_event(mark_worker_started, worker_id)
    try:
        while not stop_event.is_set():
            if monotonic() - last_heartbeat >= heartbeat_interval:
                _record_worker_event(record_worker_heartbeat, worker_id)
                last_heartbeat = monotonic()
            try:
                with SessionLocal() as session:
                    task = claim_next_async_task(
                        session,
                        worker_id=worker_id,
                        visibility_timeout_seconds=(
                            _async_task_visibility_timeout_seconds()
                        ),
                    )
                    if task is None:
                        session.commit()
                        stop_event.wait(0.5)
                        continue
                    snapshot = _snapshot_task(task)
                    session.commit()
                _record_worker_event(
                    record_worker_heartbeat,
                    worker_id,
                    current_task_request_id=str(snapshot["request_id"]),
                    current_task_type=str(snapshot["task_type"]),
                )
            except Exception as exc:  # pragma: no cover - defensive worker guard
                safe_error = safe_exception_summary(exc)
                logger.error("agent_async_worker_claim_failed error=%s", safe_error)
                _record_worker_event(record_worker_heartbeat, worker_id, last_error=safe_error)
                stop_event.wait(1)
                continue

            task_id = int(snapshot["id"])
            try:
                asyncio.run(
                    _execute_snapshot(
                        snapshot,
                        orchestrator,
                        backend_queries=backend_queries,
                    )
                )
                with SessionLocal() as session:
                    mark_callback_sent(session, task_id)
                    session.commit()
                with SessionLocal() as session:
                    mark_async_task_done(session, task_id)
                    session.commit()
                _record_worker_event(record_worker_task_completed, worker_id)
            except AgentGenerationTimeoutError as exc:
                safe_error = safe_exception_summary(exc)
                logger.error(
                    "agent_async_task_generation_timed_out "
                    "request_id=%s task_type=%s error=%s",
                    snapshot["request_id"],
                    snapshot["task_type"],
                    safe_error,
                )
                callback_envelope = _safe_failure_callback_envelope(
                    snapshot,
                    exc,
                )
                with SessionLocal() as session:
                    mark_async_task_terminal_failure(
                        session,
                        task_id,
                        safe_error,
                    )
                    if callback_envelope is not None:
                        _stage_failure_callback_delivery(
                            session,
                            task_id,
                            callback_envelope,
                            safe_error,
                        )
                    session.commit()
                _record_worker_event(
                    record_worker_heartbeat,
                    worker_id,
                    last_error=safe_error,
                )
            except Exception as exc:  # pragma: no cover - network/provider dependent
                safe_error = safe_exception_summary(exc)
                logger.error("agent_async_task_failed request_id=%s task_type=%s error=%s", snapshot["request_id"], snapshot["task_type"], safe_error)
                with SessionLocal() as session:
                    task, final_failure = mark_async_task_failed(
                        session,
                        task_id,
                        safe_error,
                    )
                    stored = task_payload(task) if task is not None else {}
                    is_callback_delivery = isinstance(
                        stored.get("_callback_payload"),
                        dict,
                    )
                    if final_failure and not is_callback_delivery:
                        callback_envelope = (
                            _safe_failure_callback_envelope(snapshot, exc)
                        )
                        if callback_envelope is not None:
                            _stage_failure_callback_delivery(
                                session,
                                task_id,
                                callback_envelope,
                                safe_error,
                            )
                    session.commit()
                _record_worker_event(record_worker_heartbeat, worker_id, last_error=safe_error)
    finally:
        _record_worker_event(mark_worker_stopped, worker_id)


def _snapshot_task(task: AgentAsyncTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "request_id": task.request_id,
        "task_type": task.task_type,
        "payload": task_payload(task),
        "callback_context": task_callback_context(task),
    }


async def _execute_snapshot(
    snapshot: dict[str, Any],
    orchestrator: AgentLangGraphNativeOrchestrator,
    *,
    backend_queries: BackendQueryTools | None = None,
) -> None:
    task_type = str(snapshot["task_type"])
    payload = snapshot["payload"]
    stored_callback = payload.get("_callback_payload")
    if isinstance(stored_callback, dict):
        callback_path = str(
            payload.get("_callback_path")
            or (
                "/api/agent/async/missed-dose-results"
                if task_type == "missed_dose"
                else ""
            )
        )
        if callback_path not in _ALLOWED_CALLBACK_PATHS:
            raise ValueError("agent_async_callback_path_invalid")
        await _post_callback(
            _callback_context(snapshot),
            callback_path,
            stored_callback,
            bearer=bool(
                payload.get(
                    "_callback_bearer",
                    task_type == "missed_dose",
                )
            ),
        )
        return
    if task_type == "push_message":
        await _post_push_message(snapshot, payload)
        return
    if task_type == "clinician_alert":
        await _post_clinician_alert(snapshot, payload)
        return
    if task_type == DAILY_PATTERN_DELIVERY_TASK:
        request = NotificationPolicyChangeProposalRequest.model_validate(
            payload,
        )
        callback_payload = _persist_callback_payload(
            int(snapshot["id"]),
            request.model_dump(mode="json"),
            callback_path=(
                "/api/agent/async/"
                "notification-policy-change-proposals"
            ),
            callback_bearer=True,
        )
        await _post_callback(
            _callback_context(snapshot),
            (
                "/api/agent/async/"
                "notification-policy-change-proposals"
            ),
            callback_payload,
            bearer=True,
        )
        return
    if task_type == DAILY_PATTERN_ANALYSIS_TASK:
        if backend_queries is None:
            raise BackendReadContractError(
                "backend_read_database_unavailable",
            )
        patient_id = str(payload.get("patient_id") or "")
        analysis_date = date.fromisoformat(
            str(payload.get("analysis_date") or ""),
        )
        pattern_payload = backend_queries.daily_pattern_context(
            patient_id=patient_id,
            analysis_date=analysis_date,
        )
        response = await _execute_traced_agent_task(
            snapshot,
            orchestrator,
            backend_queries=backend_queries,
            invocation_task_type="daily_pattern",
            invocation_payload=pattern_payload,
        )
        with SessionLocal() as session:
            enqueue_proposal_delivery(
                session,
                analysis_task_payload=payload,
                callback_context=_callback_context(snapshot).model_dump(
                    mode="json",
                ),
                response=response,
            )
            session.commit()
        return
    if task_type == "daily_pattern":
        # Daily-pattern analysis remains an AI-internal task. Its proposal
        # scheduler/callback is introduced separately once operating times are
        # approved; the removed generic result callback must not be used.
        await _execute_traced_agent_task(
            snapshot,
            orchestrator,
            backend_queries=backend_queries,
        )
        return
    if task_type == "missed_dose":
        response = await _execute_traced_agent_task(
            snapshot,
            orchestrator,
            backend_queries=backend_queries,
        )
        await _post_job_result(snapshot, response)
        return
    raise ValueError(f"unsupported_async_task_type:{task_type}")


async def _invoke_agent_with_timeout(
    orchestrator: AgentLangGraphNativeOrchestrator,
    task_type: str,
    payload: dict[str, Any],
    *,
    trace_id: str,
) -> AgentResponse:
    timeout_seconds = max(
        0.1,
        float(get_settings().llm_timeout_seconds),
    )
    try:
        return await asyncio.wait_for(
            orchestrator.invoke(
                task_type,
                payload,
                trace_id=trace_id,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise AgentGenerationTimeoutError(
            f"llm_generation_timeout:{timeout_seconds:g}s"
        ) from exc


async def _execute_traced_agent_task(
    snapshot: dict[str, Any],
    orchestrator: AgentLangGraphNativeOrchestrator,
    *,
    backend_queries: BackendQueryTools | None,
    invocation_task_type: str | None = None,
    invocation_payload: dict[str, Any] | None = None,
) -> AgentResponse:
    """Run one asynchronous Agent workflow against the Agent DB trace ledger."""

    task_type = str(snapshot["task_type"])
    original_payload = dict(snapshot["payload"])
    request_id = str(snapshot["request_id"])
    trace_id = _async_trace_id(request_id)
    trace_store = AgentTraceStore(SessionLocal)
    trace_store.start_async_task(
        request_id=request_id,
        task_type=task_type,
        payload=original_payload,
        trace_id=trace_id,
    )
    model_call_observations: list[dict[str, Any]] = []
    tool_call_observations: list[dict[str, Any]] = []
    try:
        resolved_invocation_payload = (
            invocation_payload
            if invocation_payload is not None
            else original_payload
        )
        if task_type == "missed_dose":
            resolved_invocation_payload = (
                _missed_dose_payload_with_patient_context(
                    original_payload,
                    backend_queries=backend_queries,
                )
            )
        with (
            capture_model_calls(model_call_observations),
            capture_tool_calls(tool_call_observations),
        ):
            response = await _invoke_agent_with_timeout(
                orchestrator,
                invocation_task_type or task_type,
                resolved_invocation_payload,
                trace_id=trace_id,
            )
        response = response_with_model_calls(
            response,
            model_call_observations,
        )
        trace_store.complete_async_task(
            request_id=request_id,
            patient_id=str(original_payload.get("patient_id") or ""),
            response=response,
            model_call_observations=model_call_observations,
            tool_call_observations=tool_call_observations,
        )
        return response
    except Exception as exc:
        _attach_trace_context(exc, trace_id)
        trace_store.record_model_calls(
            trace_id=trace_id,
            observations=model_call_observations,
        )
        trace_store.fail_chat(
            trace_id=trace_id,
            error_code=str(
                getattr(exc, "error_type", "")
                or type(exc).__name__
            ),
            error_message=safe_exception_summary(exc),
            retryable=bool(getattr(exc, "retryable", True)),
        )
        raise


def _async_trace_id(request_id: str) -> str:
    stable_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"dranswer-agent-async:{request_id}",
    )
    return f"async-{stable_id.hex}"


def _attach_trace_context(exc: Exception, trace_id: str) -> None:
    if getattr(exc, "trace_id", None):
        return
    try:
        exc.trace_id = trace_id
    except Exception:
        return


def _async_task_visibility_timeout_seconds() -> int:
    settings = get_settings()
    minimum = (
        ceil(max(0.1, float(settings.llm_timeout_seconds)))
        + ASYNC_CALLBACK_TIMEOUT_SECONDS
        + ASYNC_TASK_LEASE_BUFFER_SECONDS
    )
    return max(
        int(settings.agent_task_visibility_timeout_seconds),
        minimum,
    )


def _missed_dose_payload_with_patient_context(
    payload: dict[str, Any],
    *,
    backend_queries: BackendQueryTools | None,
) -> dict[str, Any]:
    if backend_queries is None:
        raise BackendReadContractError("backend_read_database_unavailable")
    # Queue rows also carry Agent-internal idempotency metadata such as
    # ``_contract_request_hash``. Rebuild the public intake DTO from its
    # three contract fields so internal queue state can never invalidate or
    # leak into the Agent invocation payload.
    request = MissedDoseEventRequest.model_validate(
        {
            "request_id": payload.get("request_id"),
            "patient_id": payload.get("patient_id"),
            "dose_event_id": payload.get("dose_event_id"),
        }
    )
    resolved = backend_queries.missed_dose_event_context(
        patient_id=request.patient_id,
        dose_event_id=request.dose_event_id,
        request_id=request.request_id,
    )
    if (
        str(resolved.get("patient_id") or "") != request.patient_id
        or str(resolved.get("dose_event_id") or "") != request.dose_event_id
    ):
        raise BackendReadContractError("backend_patient_context_scope_mismatch")
    patient_snapshot = resolved.get("patient_snapshot")
    if not isinstance(patient_snapshot, dict):
        raise BackendReadContractError("backend_patient_context_missing")
    adherence_pattern_context = resolved.get(
        "adherence_pattern_context"
    )
    tone_policy_context = resolved.get("tone_policy_context")
    if not isinstance(adherence_pattern_context, dict) or not (
        adherence_pattern_context
    ):
        raise BackendReadContractError(
            "backend_adherence_pattern_context_missing"
        )
    if not isinstance(tone_policy_context, dict) or not tone_policy_context:
        raise BackendReadContractError(
            "backend_tone_policy_context_missing"
        )
    context: dict[str, Any] = {
        "dose_event_status": str(resolved.get("status") or ""),
        "dose_event_version": int(resolved.get("version") or 1),
    }
    context["trusted_patient_context"] = patient_snapshot
    context["patient_context_snapshot"] = llm_safe_patient_snapshot(
        patient_snapshot
    )
    event = MissedDoseEventPayload(
        patient_id=request.patient_id,
        dose_event_id=request.dose_event_id,
        medication_name=str(resolved.get("medication_name") or ""),
        slot_label=str(resolved.get("slot_label") or ""),
        scheduled_for=resolved["scheduled_for"],
        detected_at=datetime.now(UTC),
        adherence_pattern_context=adherence_pattern_context,
        tone_policy_context=tone_policy_context,
        context=context,
    )
    return event.model_dump(mode="json")


async def _post_job_result(snapshot: dict[str, Any], response: AgentResponse) -> None:
    context = _callback_context(snapshot)
    if snapshot["task_type"] != "missed_dose":
        raise ValueError("unsupported_external_async_result_type")
    payload = MissedDoseResultCallback(
        request_id=snapshot["request_id"],
        status="completed",
        result=MissedDoseCallbackResult(
            message=response.human_summary,
            requires_reply=bool(response.requires_conversation_alert),
        ),
        error=None,
    )
    callback_payload = _persist_callback_payload(
        int(snapshot["id"]),
        payload.model_dump(mode="json"),
        callback_path="/api/agent/async/missed-dose-results",
        callback_bearer=True,
    )
    await _post_callback(
        context,
        "/api/agent/async/missed-dose-results",
        callback_payload,
        bearer=True,
    )


async def _post_push_message(snapshot: dict[str, Any], payload: dict[str, Any]) -> None:
    context = _callback_context(snapshot)
    request = AgentAsyncPushMessageRequest.model_validate(
        {
            **payload,
            "request_id": payload.get("request_id") or snapshot["request_id"],
            "idempotency_key": payload.get("idempotency_key") or f"{snapshot['request_id']}:push-message",
        }
    )
    callback_payload = _persist_callback_payload(
        int(snapshot["id"]),
        request.model_dump(mode="json", by_alias=True),
        callback_path="/api/agent/async/push-messages",
        callback_bearer=False,
    )
    await _post_callback(
        context,
        "/api/agent/async/push-messages",
        callback_payload,
    )


async def _post_clinician_alert(snapshot: dict[str, Any], payload: dict[str, Any]) -> None:
    context = _callback_context(snapshot)
    request = AgentAsyncClinicianAlertRequest.model_validate(
        {
            **payload,
            "request_id": payload.get("request_id") or snapshot["request_id"],
            "idempotency_key": payload.get("idempotency_key") or f"{snapshot['request_id']}:clinician-alert",
        }
    )
    callback_payload = _persist_callback_payload(
        int(snapshot["id"]),
        request.model_dump(mode="json"),
        callback_path="/api/agent/async/clinician-alerts",
        callback_bearer=False,
    )
    await _post_callback(
        context,
        "/api/agent/async/clinician-alerts",
        callback_payload,
    )


def _failure_callback_envelope(
    snapshot: dict[str, Any],
    exc: Exception,
) -> tuple[str, dict[str, Any], bool] | None:
    if snapshot["task_type"] != "missed_dose":
        return None
    public_error = public_processing_error(exc)
    payload = MissedDoseResultCallback(
        request_id=snapshot["request_id"],
        status="failed",
        result=None,
        error=AsyncProcessingError(
            code=public_error.code,
            message=public_error.message,
            retryable=public_error.retryable,
        ),
    )
    return (
        "/api/agent/async/missed-dose-results",
        payload.model_dump(mode="json"),
        True,
    )


def _safe_failure_callback_envelope(
    snapshot: dict[str, Any],
    exc: Exception,
) -> tuple[str, dict[str, Any], bool] | None:
    try:
        return _failure_callback_envelope(snapshot, exc)
    except Exception as callback_exc:  # pragma: no cover - corrupt task guard
        logger.error(
            "agent_async_failure_callback_stage_failed "
            "request_id=%s task_type=%s error=%s",
            snapshot.get("request_id"),
            snapshot.get("task_type"),
            safe_exception_summary(callback_exc),
        )
        return None


def _stage_failure_callback_delivery(
    session,
    task_id: int,
    callback_envelope: tuple[str, dict[str, Any], bool],
    processing_error: str,
) -> None:
    callback_path, callback_payload, callback_bearer = callback_envelope
    stage_async_task_callback_delivery(
        session,
        task_id,
        callback_payload=callback_payload,
        callback_path=callback_path,
        callback_bearer=callback_bearer,
        processing_error=processing_error,
    )


def _callback_context(snapshot: dict[str, Any]) -> AgentCallbackContext:
    raw = snapshot.get("callback_context")
    if isinstance(raw, dict) and raw:
        return AgentCallbackContext.model_validate(raw)
    raw_from_payload = snapshot.get("payload", {}).get("callback_context")
    if isinstance(raw_from_payload, dict) and raw_from_payload:
        return AgentCallbackContext.model_validate(raw_from_payload)
    return AgentCallbackContext(app_base_url=get_settings().system_base_url)


def _persist_callback_payload(
    task_id: int,
    callback_payload: dict[str, Any],
    *,
    callback_path: str = "/api/agent/async/missed-dose-results",
    callback_bearer: bool = True,
) -> dict[str, Any]:
    """Persist once so a lost ACK retries the byte-equivalent callback."""

    with SessionLocal() as session:
        task = session.get(AgentAsyncTask, task_id)
        if task is None:
            raise ValueError("agent_async_task_not_found")
        stored_payload = task_payload(task)
        existing = stored_payload.get("_callback_payload")
        if isinstance(existing, dict):
            return existing
        stored_payload["_callback_payload"] = callback_payload
        stored_payload["_callback_path"] = callback_path
        stored_payload["_callback_bearer"] = callback_bearer
        task.payload_json = json.dumps(
            stored_payload,
            ensure_ascii=False,
        )
        session.commit()
    return callback_payload


async def _post_callback(
    context: AgentCallbackContext,
    path: str,
    payload: dict[str, Any],
    *,
    bearer: bool = False,
) -> None:
    settings = get_settings()
    headers = (
        {
            "Authorization": (
                f"Bearer {settings.require_service_api_token()}"
            )
        }
        if bearer
        else {
            "X-Internal-Api-Token": settings.require_internal_api_token()
        }
    )
    base_url = context.app_base_url.rstrip("/")
    async with httpx.AsyncClient(
        timeout=float(ASYNC_CALLBACK_TIMEOUT_SECONDS),
        trust_env=False,
    ) as client:
        response = await client.post(f"{base_url}{path}", json=payload, headers=headers)
        response.raise_for_status()
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise ValueError("agent_async_callback_ack_invalid") from exc
        if path == "/api/agent/async/missed-dose-results":
            ack = AsyncResultCallbackAck.model_validate(response_payload)
            if ack.request_id != str(payload.get("request_id") or ""):
                raise ValueError(
                    "agent_async_callback_ack_request_id_mismatch"
                )
            return
        if (
            path
            == "/api/agent/async/notification-policy-change-proposals"
        ):
            ack = NotificationPolicyProposalAck.model_validate(
                response_payload,
            )
            if ack.request_id != str(payload.get("request_id") or ""):
                raise ValueError(
                    "agent_async_callback_ack_request_id_mismatch"
                )
            return
        if (
            not isinstance(response_payload, dict)
            or response_payload.get("request_id")
            != payload.get("request_id")
            or response_payload.get("status") not in {"ok", "duplicate"}
        ):
            raise ValueError("agent_async_callback_ack_invalid")


def _record_worker_event(event_func, worker_id: str, **kwargs: Any) -> None:
    try:
        with SessionLocal() as session:
            event_func(session, worker_id, **kwargs)
            session.commit()
    except Exception as exc:  # pragma: no cover - status reporting must not stop work
        logger.warning("agent_worker_status_update_failed worker_id=%s error=%s", worker_id, safe_exception_summary(exc))
