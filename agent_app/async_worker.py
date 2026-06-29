from __future__ import annotations

import asyncio
import logging
import socket
import threading
import uuid
from time import monotonic
from typing import Any

import httpx

from agent_app.async_tasks import (
    claim_next_async_task,
    mark_async_task_done,
    mark_async_task_failed,
    mark_callback_sent,
    task_callback_context,
    task_payload,
)
from agent_app.db import SessionLocal
from agent_app.graph import AgentLangGraphNativeOrchestrator
from agent_app.models import AgentAsyncTask
from agent_app.worker_status import (
    mark_worker_started,
    mark_worker_stopped,
    record_worker_heartbeat,
    record_worker_task_completed,
)
from shared.schemas import (
    AgentAsyncChatResultRequest,
    AgentAsyncClinicianAlertRequest,
    AgentAsyncFailureRequest,
    AgentAsyncJobResultRequest,
    AgentAsyncPolicyChangeRequest,
    AgentAsyncPushMessageRequest,
    AgentCallbackContext,
    AgentResponse,
)
from shared.redaction import safe_exception_summary
from shared.settings import get_settings

logger = logging.getLogger("uvicorn.error")


def async_task_worker(stop_event: threading.Event, orchestrator: AgentLangGraphNativeOrchestrator) -> None:
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
                    task = claim_next_async_task(session, worker_id=worker_id)
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
                asyncio.run(_execute_snapshot(snapshot, orchestrator))
                with SessionLocal() as session:
                    mark_callback_sent(session, task_id)
                    session.commit()
                with SessionLocal() as session:
                    mark_async_task_done(session, task_id)
                    session.commit()
                _record_worker_event(record_worker_task_completed, worker_id)
            except Exception as exc:  # pragma: no cover - network/provider dependent
                safe_error = safe_exception_summary(exc)
                logger.error("agent_async_task_failed request_id=%s task_type=%s error=%s", snapshot["request_id"], snapshot["task_type"], safe_error)
                with SessionLocal() as session:
                    _task, final_failure = mark_async_task_failed(session, task_id, safe_error)
                    session.commit()
                _record_worker_event(record_worker_heartbeat, worker_id, last_error=safe_error)
                if final_failure:
                    with contextlib_suppress_callback_errors():
                        asyncio.run(_post_failure(snapshot, exc))
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


async def _execute_snapshot(snapshot: dict[str, Any], orchestrator: AgentLangGraphNativeOrchestrator) -> None:
    task_type = str(snapshot["task_type"])
    payload = snapshot["payload"]
    if task_type == "push_message":
        await _post_push_message(snapshot, payload)
        return
    if task_type == "clinician_alert":
        await _post_clinician_alert(snapshot, payload)
        return
    if task_type == "daily_pattern":
        response = await orchestrator.invoke("daily_pattern", payload)
        await _post_job_result(snapshot, response)
        return
    if task_type == "missed_dose":
        response = await orchestrator.invoke("missed_dose", payload)
        await _post_job_result(snapshot, response)
        return
    if task_type == "chat_continuation":
        response = await orchestrator.invoke("multiturn_chat", payload)
        if _requires_async_continuation(response):
            response = await orchestrator.invoke("multiturn_chat", _continuation_payload(payload, response))
        if _is_policy_change_response(response):
            await _post_policy_change(snapshot, response)
        else:
            await _post_chat_result(snapshot, response)
        return
    raise ValueError(f"unsupported_async_task_type:{task_type}")


def _is_policy_change_response(response: AgentResponse) -> bool:
    if response.structured_payload.get("policy_confirmation_required") is True:
        return True
    tool_call = response.structured_payload.get("tool_call")
    if isinstance(tool_call, dict) and tool_call.get("name") in {"apply_notification_policy", "apply_system_policy"}:
        return True
    tool_calls = response.structured_payload.get("tool_calls")
    return isinstance(tool_calls, list) and any(
        isinstance(item, dict) and item.get("name") in {"apply_notification_policy", "apply_system_policy"} for item in tool_calls
    )


def _requires_async_continuation(response: AgentResponse) -> bool:
    return response.structured_payload.get("async_continuation_required") is True


def _continuation_payload(payload: dict[str, Any], response: AgentResponse) -> dict[str, Any]:
    continuation = dict(payload)
    context = dict(continuation.get("context") or {})
    context["execute_async_continuation"] = True
    context["async_continuation_type"] = response.structured_payload.get("async_continuation_type", "")
    context["async_tool_calls"] = response.structured_payload.get("tool_calls", [])
    continuation["context"] = context
    return continuation


async def _post_job_result(snapshot: dict[str, Any], response: AgentResponse) -> None:
    context = _callback_context(snapshot)
    payload = AgentAsyncJobResultRequest(
        request_id=snapshot["request_id"],
        task_type=snapshot["task_type"],
        response=response,
        job_id=context.job_id,
        related_dose_event_id=_related_dose_event_id(snapshot["payload"]),
        idempotency_key=f"{snapshot['request_id']}:job-result",
    )
    await _post_callback(context, "/api/agent/async/job-results", payload.model_dump(mode="json"))


async def _post_chat_result(snapshot: dict[str, Any], response: AgentResponse) -> None:
    context = _callback_context(snapshot)
    payload = AgentAsyncChatResultRequest(
        request_id=snapshot["request_id"],
        event_type=str(snapshot["payload"].get("event_type") or "multiturn_chat"),
        message=str(snapshot["payload"].get("message") or ""),
        notification_id=context.notification_id,
        response=response,
        idempotency_key=f"{snapshot['request_id']}:chat-result",
    )
    await _post_callback(context, "/api/agent/async/chat-results", payload.model_dump(mode="json"))


async def _post_policy_change(snapshot: dict[str, Any], response: AgentResponse) -> None:
    context = _callback_context(snapshot)
    payload = AgentAsyncPolicyChangeRequest(
        request_id=snapshot["request_id"],
        source_event_type=str(snapshot["payload"].get("event_type") or "multiturn_chat"),
        notification_id=context.notification_id,
        response=response,
        idempotency_key=f"{snapshot['request_id']}:policy-change",
    )
    await _post_callback(context, "/api/agent/async/policy-change-requests", payload.model_dump(mode="json"))


async def _post_push_message(snapshot: dict[str, Any], payload: dict[str, Any]) -> None:
    context = _callback_context(snapshot)
    request = AgentAsyncPushMessageRequest.model_validate(
        {
            **payload,
            "request_id": payload.get("request_id") or snapshot["request_id"],
            "idempotency_key": payload.get("idempotency_key") or f"{snapshot['request_id']}:push-message",
        }
    )
    await _post_callback(context, "/api/agent/async/push-messages", request.model_dump(mode="json", by_alias=True))


async def _post_clinician_alert(snapshot: dict[str, Any], payload: dict[str, Any]) -> None:
    context = _callback_context(snapshot)
    request = AgentAsyncClinicianAlertRequest.model_validate(
        {
            **payload,
            "request_id": payload.get("request_id") or snapshot["request_id"],
            "idempotency_key": payload.get("idempotency_key") or f"{snapshot['request_id']}:clinician-alert",
        }
    )
    await _post_callback(context, "/api/agent/async/clinician-alerts", request.model_dump(mode="json"))


async def _post_failure(snapshot: dict[str, Any], exc: Exception) -> None:
    context = _callback_context(snapshot)
    payload = AgentAsyncFailureRequest(
        request_id=snapshot["request_id"],
        task_type=snapshot["task_type"],
        message=safe_exception_summary(exc),
        error_type=str(getattr(exc, "error_type", "agent_async_task_failed") or "agent_async_task_failed"),
        trace_id=getattr(exc, "trace_id", None),
        agent_name=getattr(exc, "agent_name", None),
        decision_type=getattr(exc, "decision_type", None),
        job_id=context.job_id,
        notification_id=context.notification_id,
        related_dose_event_id=_related_dose_event_id(snapshot["payload"]),
        idempotency_key=f"{snapshot['request_id']}:failure",
    )
    await _post_callback(context, "/api/agent/async/failures", payload.model_dump(mode="json"))


def _callback_context(snapshot: dict[str, Any]) -> AgentCallbackContext:
    raw = snapshot.get("callback_context")
    if isinstance(raw, dict) and raw:
        return AgentCallbackContext.model_validate(raw)
    raw_from_payload = snapshot.get("payload", {}).get("callback_context")
    if isinstance(raw_from_payload, dict) and raw_from_payload:
        return AgentCallbackContext.model_validate(raw_from_payload)
    return AgentCallbackContext(app_base_url=get_settings().system_base_url)


def _related_dose_event_id(payload: dict[str, Any]) -> int | None:
    for key in ("dose_event_id", "related_dose_event_id"):
        value = payload.get(key)
        if isinstance(value, int):
            return int(value)
    return None


async def _post_callback(context: AgentCallbackContext, path: str, payload: dict[str, Any]) -> None:
    settings = get_settings()
    headers = {"X-Internal-Api-Token": settings.internal_api_token} if settings.internal_api_token else {}
    base_url = context.app_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.post(f"{base_url}{path}", json=payload, headers=headers)
        response.raise_for_status()


class contextlib_suppress_callback_errors:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc is not None:
            logger.warning("agent_async_failure_callback_failed error=%s", safe_exception_summary(exc))
        return True


def _record_worker_event(event_func, worker_id: str, **kwargs: Any) -> None:
    try:
        with SessionLocal() as session:
            event_func(session, worker_id, **kwargs)
            session.commit()
    except Exception as exc:  # pragma: no cover - status reporting must not stop work
        logger.warning("agent_worker_status_update_failed worker_id=%s error=%s", worker_id, safe_exception_summary(exc))
