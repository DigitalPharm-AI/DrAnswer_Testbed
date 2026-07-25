from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from agent_app import trace_logging
from agent_app.errors import AgentExecutionError
from agent_app.integration.chat_contracts import (
    ChatSyncRequest,
    ChatSyncResponse,
    agent_chat_payload,
    chat_error,
    chat_sync_response,
)
from agent_app.integration.idempotency import (
    ConversationBusyError,
    RequestInProgressError,
    SyncRequestGate,
    SyncRequestGateError,
)
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.persistence.trace_store import AgentTraceStore
from agent_app.security import require_agent_sync_bearer_token, require_internal_api_token
from agent_app.tools.backend_query import BackendChatMessageNotFound, BackendQueryTools
from shared.redaction import safe_exception_summary
from shared.schemas import AgentResponse, MultiturnChatRequest, MutationConfirmationResolutionRequest
from shared.settings import get_settings

router = APIRouter()
SYNC_CHAT_PATH = "/agent/sync/chat"

_orchestrator_getter: Callable[[], AgentLangGraphNativeOrchestrator] | None = None
_sync_session_factory_getter: Callable[[], sessionmaker[Session]] | None = None
_backend_query_tools_getter: Callable[[], BackendQueryTools | None] | None = None


def configure_orchestrator(getter: Callable[[], AgentLangGraphNativeOrchestrator]) -> None:
    global _orchestrator_getter
    _orchestrator_getter = getter


def configure_sync_session_factory(getter: Callable[[], sessionmaker[Session]]) -> None:
    global _sync_session_factory_getter
    _sync_session_factory_getter = getter


def configure_backend_query_tools(getter: Callable[[], BackendQueryTools | None]) -> None:
    global _backend_query_tools_getter
    _backend_query_tools_getter = getter


def _orchestrator() -> AgentLangGraphNativeOrchestrator:
    if _orchestrator_getter is None:
        raise RuntimeError("agent_orchestrator_not_configured")
    return _orchestrator_getter()


def _sync_request_gate() -> SyncRequestGate:
    if _sync_session_factory_getter is None:
        raise RuntimeError("agent_sync_session_factory_not_configured")
    settings = get_settings()
    return SyncRequestGate(
        _sync_session_factory_getter(),
        api_path=SYNC_CHAT_PATH,
        lock_lease_seconds=settings.agent_sync_lock_lease_seconds,
        retention_seconds=settings.agent_sync_request_retention_seconds,
    )


def _trace_store() -> AgentTraceStore:
    if _sync_session_factory_getter is None:
        raise RuntimeError("agent_sync_session_factory_not_configured")
    return AgentTraceStore(_sync_session_factory_getter(), settings=get_settings())


def _backend_query_tools() -> BackendQueryTools | None:
    if _backend_query_tools_getter is None:
        return None
    return _backend_query_tools_getter()


@router.post(
    SYNC_CHAT_PATH,
    response_model=ChatSyncResponse,
    dependencies=[Depends(require_agent_sync_bearer_token)],
)
async def sync_chat(payload: ChatSyncRequest) -> JSONResponse:
    settings = get_settings()
    gate = _sync_request_gate()
    trace_store = _trace_store()
    trace_logging.log_info(
        "agent_api_call",
        path=SYNC_CHAT_PATH,
        mode="sync",
        task_type="multiturn_chat",
        request_id=payload.request_id,
        conversation_id=payload.conversation_id,
        message_id=payload.message_id,
    )
    try:
        replay = gate.begin(payload)
    except SyncRequestGateError as exc:
        headers = None
        if isinstance(exc, (RequestInProgressError, ConversationBusyError)):
            headers = {"Retry-After": str(max(1, settings.agent_sync_retry_after_seconds))}
        body = chat_error(
            exc.code,
            exc.message,
            retryable=exc.retryable,
            details=exc.details,
        ).model_dump(mode="json")
        return JSONResponse(status_code=409, content=body, headers=headers)

    if replay is not None:
        return JSONResponse(status_code=replay.status_code, content=replay.body)

    internal_trace_id = str(uuid4())
    backend_context: dict[str, Any] = {}
    backend_queries = _backend_query_tools()
    if backend_queries is not None:
        try:
            backend_context = await asyncio.to_thread(
                backend_queries.validate_chat_message,
                payload,
            )
        except BackendChatMessageNotFound:
            return _final_chat_error(
                gate,
                trace_store,
                payload,
                code="BACKEND_MESSAGE_NOT_FOUND",
                message="The Backend chat message could not be verified.",
                trace_id=internal_trace_id,
                status_code=404,
                retryable=False,
            )
        except Exception as exc:
            trace_logging.log_info(
                "agent_backend_read_failed",
                request_id=payload.request_id,
                error=safe_exception_summary(exc, limit=300),
            )
            return _final_chat_error(
                gate,
                trace_store,
                payload,
                code="BACKEND_DB_UNAVAILABLE",
                message="The Backend read database is temporarily unavailable.",
                trace_id=internal_trace_id,
                status_code=503,
                retryable=True,
            )
    try:
        gate.bind_trace(payload, internal_trace_id)
        trace_store.start_chat(
            payload,
            trace_id=internal_trace_id,
            api_path=SYNC_CHAT_PATH,
        )
        agent_response = await asyncio.wait_for(
            _orchestrator().invoke(
                "multiturn_chat",
                agent_chat_payload(payload, backend_context=backend_context),
                trace_id=internal_trace_id,
            ),
            timeout=settings.agent_sync_chat_timeout_seconds,
        )
        if agent_response.trace_id != internal_trace_id:
            raise RuntimeError("agent_trace_id_mismatch")
        external_response = chat_sync_response(payload, agent_response)
        body = external_response.model_dump(mode="json")
        trace_store.complete_chat(payload, agent_response)
        gate.complete(
            payload,
            status_code=200,
            body=body,
            trace_id=agent_response.trace_id,
        )
        return JSONResponse(status_code=200, content=body)
    except TimeoutError:
        return _final_chat_error(
            gate,
            trace_store,
            payload,
            code="AI_PROCESSING_ERROR",
            message="The AI request exceeded the processing time limit.",
            trace_id=internal_trace_id,
        )
    except AgentExecutionError as exc:
        trace_logging.log_info(
            "agent_sync_chat_failed",
            request_id=payload.request_id,
            trace_id=exc.trace_id,
            error_type=exc.error_type,
            error=safe_exception_summary(exc, limit=300),
        )
        return _final_chat_error(
            gate,
            trace_store,
            payload,
            code="AI_PROCESSING_ERROR",
            message="An internal AI processing error occurred.",
            trace_id=internal_trace_id,
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_sync_chat_failed",
            request_id=payload.request_id,
            error=safe_exception_summary(exc, limit=300),
        )
        return _final_chat_error(
            gate,
            trace_store,
            payload,
            code="AI_PROCESSING_ERROR",
            message="An internal AI processing error occurred.",
            trace_id=internal_trace_id,
        )


def _final_chat_error(
    gate: SyncRequestGate,
    trace_store: AgentTraceStore,
    payload: ChatSyncRequest,
    *,
    code: str,
    message: str,
    trace_id: str = "",
    status_code: int = 500,
    retryable: bool = False,
) -> JSONResponse:
    body = chat_error(
        code,
        message,
        retryable=retryable,
        details=None,
    ).model_dump(mode="json")
    try:
        trace_store.fail_chat(
            trace_id=trace_id,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_trace_persistence_failed",
            request_id=payload.request_id,
            trace_id=trace_id,
            error=safe_exception_summary(exc, limit=300),
        )
    gate.fail(
        payload,
        status_code=status_code,
        body=body,
        trace_id=trace_id,
        error_code=code,
        retryable=retryable,
    )
    return JSONResponse(status_code=status_code, content=body)


@router.post("/agent/multiturn-chat", response_model=AgentResponse, dependencies=[Depends(require_internal_api_token)])
async def multiturn_chat(payload: MultiturnChatRequest) -> AgentResponse:
    trace_logging.log_info("agent_api_call", path="/agent/multiturn-chat", mode="sync", task_type="multiturn_chat")
    return await _orchestrator().invoke("multiturn_chat", payload.model_dump(mode="json"))


@router.post(
    "/agent/mutation-confirmations/resolve",
    response_model=AgentResponse,
    dependencies=[Depends(require_internal_api_token)],
)
async def resolve_mutation_confirmation(
    payload: MutationConfirmationResolutionRequest,
) -> AgentResponse:
    orchestrator = _orchestrator()
    request_payload = payload.original_request.model_dump(mode="json")
    resolution_status = "cancelled"
    tool_result: dict[str, Any] = {}
    if payload.resolution == "confirm":
        execution_payload = dict(request_payload)
        execution_context = dict(execution_payload.get("context") or {})
        execution_context["approved_mutation_confirmation"] = {
            "confirmation_id": payload.confirmation_id,
            "action_name": payload.action_name,
            "action_fingerprint": payload.action_fingerprint,
        }
        execution_payload["context"] = execution_context
        calls, results = await orchestrator.tool_runtime.execute(
            [
                {
                    "id": payload.tool_call_id or f"{payload.confirmation_id}:confirmed",
                    "name": payload.action_name,
                    "arguments": payload.arguments,
                }
            ],
            trace_id=f"mutation-confirmation:{payload.confirmation_id}",
            source_event_type=payload.source_event_type,
            payload=execution_payload,
            routing_context={
                "routing_mode": "confirmed_mutation",
                "executed_by": payload.source_event_type,
                "tool_names": [payload.action_name],
                "agent_graph_mode": "langgraph_state_graph",
            },
        )
        if len(calls) != 1 or len(results) != 1:
            raise AgentExecutionError(
                "confirmed_mutation_execution_result_count_mismatch",
                error_type="confirmed_mutation_execution_failed",
                trace_id=f"mutation-confirmation:{payload.confirmation_id}",
                agent_name="mutation_confirmation_executor",
                decision_type=payload.action_name,
            )
        result = results[0]
        tool_result = result.model_dump(mode="json")
        if result.status == "success":
            resolution_status = "applied"
        elif result.error == "mutation_confirmation_stale":
            resolution_status = "stale"
        else:
            resolution_status = "failed"

    context = dict(request_payload.get("context") or {})
    context.pop("approved_mutation_confirmation", None)
    context["mutation_resolution"] = {
        "confirmation_id": payload.confirmation_id,
        "status": resolution_status,
        "action_type": payload.action_type,
        "action_name": payload.action_name,
        "action_fingerprint": payload.action_fingerprint,
        "tool_result": tool_result,
    }
    request_payload["context"] = context
    trace_logging.log_info(
        "agent_mutation_confirmation_resolved",
        confirmation_id=payload.confirmation_id,
        action_name=payload.action_name,
        resolution=payload.resolution,
        status=resolution_status,
    )
    return await orchestrator.invoke("multiturn_chat", request_payload)
