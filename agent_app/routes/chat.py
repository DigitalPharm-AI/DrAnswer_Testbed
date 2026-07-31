from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from agent_app import trace_logging
from agent_app.errors import AgentExecutionError, public_processing_error
from agent_app.integration.approval_state import (
    InternalApprovalDecision,
    InternalApprovalEncryptionError,
    InternalApprovalError,
    InternalApprovalStore,
)
from agent_app.integration.idempotency import (
    PatientThreadBusyError,
    RequestInProgressError,
    StoredHttpResponse,
    SyncRequestClaim,
    SyncRequestGate,
    SyncRequestGateError,
)
from agent_app.integration.pro_ctcae_survey import (
    ProCtcaeSurveyEncryptionError,
    ProCtcaeSurveyError,
    ProCtcaeSurveyService,
    ProCtcaeSurveyTransition,
    pro_ctcae_question_response,
)
from agent_app.integration.selection_state import (
    FoodSelectionStateStore,
    ResolvedFoodSelection,
    SelectionStateEncryptionError,
    SelectionStateError,
    food_selection_question_response,
)
from agent_app.observability.model_calls import (
    capture_model_calls,
    response_with_model_calls,
)
from agent_app.orchestration.continuation import (
    resolve_required_continuations,
)
from agent_app.orchestration.graph import (
    AgentLangGraphNativeOrchestrator,
)
from agent_app.persistence.trace_store import AgentTraceStore
from agent_app.security import require_agent_sync_bearer_token
from agent_app.streaming import publish_agent_text_with
from agent_app.tools.backend_query import (
    BackendChatMessageNotFound,
    BackendQueryTools,
)
from shared.chat_contracts import (
    ChatContractError,
    ChatErrorResponse,
    ChatStreamEvent,
    ChatSyncRequest,
    ChatSyncResponse,
    agent_chat_payload,
    chat_error,
    chat_sync_response,
)
from shared.redaction import safe_exception_summary
from shared.schemas import AgentResponse
from shared.settings import get_settings
from shared.tool_names import (
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    REQUEST_RECORD_APPROVAL,
)

router = APIRouter()
SYNC_CHAT_PATH = "/agent/sync/chat"
NDJSON_MEDIA_TYPE = "application/x-ndjson"
STORED_STREAM_EVENTS_KEY = "_chat_stream_events_v1"
SYNC_CHAT_RESPONSES = {
    400: {
        "model": ChatErrorResponse,
        "description": (
            "Request schema, required field, or Accept validation failed."
        ),
    },
    401: {
        "model": ChatErrorResponse,
        "description": "Bearer authentication failed.",
    },
    404: {
        "model": ChatErrorResponse,
        "description": "The Backend user message could not be verified.",
    },
    409: {
        "model": ChatErrorResponse,
        "description": "Idempotency or patient-thread lock conflict.",
    },
    500: {
        "model": ChatErrorResponse,
        "description": "Unexpected AI Server processing error.",
    },
    503: {
        "model": ChatErrorResponse,
        "description": (
            "Backend read database is unavailable or incompatible."
        ),
    },
    504: {
        "model": ChatErrorResponse,
        "description": "AI Server processing timed out.",
    },
}

_orchestrator_getter: (
    Callable[[], AgentLangGraphNativeOrchestrator] | None
) = None
_sync_session_factory_getter: (
    Callable[[], sessionmaker[Session]] | None
) = None
_backend_query_tools_getter: (
    Callable[[], BackendQueryTools | None] | None
) = None


def configure_orchestrator(
    getter: Callable[[], AgentLangGraphNativeOrchestrator],
) -> None:
    global _orchestrator_getter
    _orchestrator_getter = getter


def configure_sync_session_factory(
    getter: Callable[[], sessionmaker[Session]],
) -> None:
    global _sync_session_factory_getter
    _sync_session_factory_getter = getter


def configure_backend_query_tools(
    getter: Callable[[], BackendQueryTools | None],
) -> None:
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
    return AgentTraceStore(
        _sync_session_factory_getter(),
        settings=get_settings(),
    )


def _pro_ctcae_survey_service() -> ProCtcaeSurveyService:
    if _sync_session_factory_getter is None:
        raise RuntimeError("agent_sync_session_factory_not_configured")
    return ProCtcaeSurveyService(
        _sync_session_factory_getter(),
        settings=get_settings(),
    )


def _internal_approval_store() -> InternalApprovalStore:
    if _sync_session_factory_getter is None:
        raise RuntimeError("agent_sync_session_factory_not_configured")
    return InternalApprovalStore(
        _sync_session_factory_getter(),
        settings=get_settings(),
    )


def _food_selection_store() -> FoodSelectionStateStore:
    if _sync_session_factory_getter is None:
        raise RuntimeError(
            "agent_sync_session_factory_not_configured"
        )
    return FoodSelectionStateStore(
        _sync_session_factory_getter(),
        settings=get_settings(),
    )


def _backend_query_tools() -> BackendQueryTools | None:
    if _backend_query_tools_getter is None:
        return None
    return _backend_query_tools_getter()


async def _invoke_sync_chat(
    *,
    orchestrator: AgentLangGraphNativeOrchestrator,
    agent_payload: dict[str, Any],
    trace_id: str,
) -> AgentResponse:
    async def invoke(
        current_payload: dict[str, Any],
    ) -> AgentResponse:
        response = await orchestrator.invoke(
            "multiturn_chat",
            current_payload,
            trace_id=trace_id,
        )
        if response.trace_id != trace_id:
            raise RuntimeError("agent_trace_id_mismatch")
        return response

    initial_response = await invoke(agent_payload)
    return await resolve_required_continuations(
        agent_payload,
        initial_response,
        invoke,
    )


async def _invoke_sync_chat_contract(
    *,
    orchestrator: AgentLangGraphNativeOrchestrator,
    agent_payload: dict[str, Any],
    payload: ChatSyncRequest,
    trace_id: str,
) -> tuple[AgentResponse, ChatSyncResponse]:
    survey_service = _pro_ctcae_survey_service()
    selection_store = _food_selection_store()
    resolved_food_selection: ResolvedFoodSelection | None = None
    try:
        approval_decision = await _submit_record_approval_if_present(
            payload=payload,
            agent_payload=agent_payload,
        )
        if approval_decision is not None:
            if approval_decision.kind == "cancelled":
                agent_response = _cancelled_approval_response(
                    trace_id=trace_id,
                    action_name=approval_decision.action_name,
                )
                await asyncio.to_thread(
                    survey_service.resolve_approval,
                    patient_id=payload.patient_id,
                    applied=False,
                )
            else:
                context = dict(agent_payload.get("context") or {})
                context["approved_user_action"] = {
                    "status": "confirmed",
                    "action_name": approval_decision.action_name,
                    "arguments": {
                        "approval_key": approval_decision.approval_key,
                    },
                }
                agent_response = await _invoke_sync_chat(
                    orchestrator=orchestrator,
                    agent_payload={**agent_payload, "context": context},
                    trace_id=trace_id,
                )
                await asyncio.to_thread(
                    survey_service.resolve_approval,
                    patient_id=payload.patient_id,
                    applied=True,
                )
        else:
            transition = await _submit_pro_ctcae_response_if_present(
                survey_service,
                payload=payload,
                agent_payload=agent_payload,
            )
            if transition is not None:
                agent_response = await _agent_response_for_survey_transition(
                    survey_service,
                    orchestrator=orchestrator,
                    payload=payload,
                    agent_payload=agent_payload,
                    trace_id=trace_id,
                    transition=transition,
                )
            else:
                resolved_food_selection = await asyncio.to_thread(
                    _resolve_food_selection_if_present,
                    selection_store,
                    payload=payload,
                    agent_payload=agent_payload,
                )
                if resolved_food_selection is not None:
                    if (
                        resolved_food_selection.kind
                        == "next_selection"
                    ):
                        agent_response = (
                            food_selection_question_response(
                                resolved_food_selection,
                                trace_id=trace_id,
                            )
                        )
                    else:
                        record_arguments = (
                            resolved_food_selection
                            .record_arguments()
                        )
                        agent_response = await (
                            orchestrator
                            .continue_nutrition_food_selection(
                                trace_id=trace_id,
                                payload=agent_payload,
                                record_arguments=(
                                    record_arguments
                                ),
                                selection_id=(
                                    resolved_food_selection
                                    .selection_id
                                ),
                                origin_message_id=(
                                    resolved_food_selection
                                    .origin_message_id
                                ),
                            )
                        )
                        if isinstance(
                            agent_response
                            .structured_payload.get(
                                "mutation_confirmation"
                            ),
                            dict,
                        ):
                            await asyncio.to_thread(
                                selection_store.consume,
                                patient_id=(
                                    payload.patient_id
                                ),
                                origin_message_id=(
                                    resolved_food_selection
                                    .origin_message_id
                                ),
                                current_user_message_id=(
                                    payload.message_id
                                ),
                            )
                else:
                    agent_response = await _invoke_sync_chat(
                        orchestrator=orchestrator,
                        agent_payload=agent_payload,
                        trace_id=trace_id,
                    )
                started = await asyncio.to_thread(
                    survey_service.start_from_agent_response,
                    patient_id=payload.patient_id,
                    origin_message_id=payload.message_id,
                    trace_id=trace_id,
                    user_message=payload.message,
                    response=agent_response,
                )
                if started is not None:
                    agent_response = (
                        await _agent_response_for_survey_transition(
                            survey_service,
                            orchestrator=orchestrator,
                            payload=payload,
                            agent_payload=agent_payload,
                            trace_id=trace_id,
                            transition=started,
                        )
                    )
        await asyncio.to_thread(
            selection_store.prepare_from_agent_response,
            patient_id=payload.patient_id,
            origin_message_id=payload.message_id,
            source_chat_request_id=payload.request_id,
            trace_id=trace_id,
            message_at=payload.message_at,
            response=agent_response,
        )
    except (
        InternalApprovalError,
        InternalApprovalEncryptionError,
    ) as exc:
        raise AgentExecutionError(
            str(exc),
            error_type="internal_approval_state_failed",
            trace_id=trace_id,
            agent_name="internal_approval_state",
            decision_type="record_approval",
        ) from exc
    except (
        SelectionStateError,
        SelectionStateEncryptionError,
    ) as exc:
        raise AgentExecutionError(
            str(exc),
            error_type="food_selection_state_failed",
            trace_id=trace_id,
            agent_name="food_selection_state",
            decision_type="food_selection_continuation",
        ) from exc
    except ProCtcaeSurveyError as exc:
        raise AgentExecutionError(
            exc.code,
            error_type=(
                exc.code
                if exc.code
                in {
                    "pro_ctcae_survey_response_invalid",
                    "pro_ctcae_survey_stale_response",
                    "pro_ctcae_survey_expired",
                }
                else "pro_ctcae_survey_state_failed"
            ),
            trace_id=trace_id,
            agent_name="pro_ctcae_survey_state",
            decision_type="pro_ctcae_survey",
        ) from exc
    except ProCtcaeSurveyEncryptionError as exc:
        raise AgentExecutionError(
            str(exc),
            error_type="pro_ctcae_survey_state_failed",
            trace_id=trace_id,
            agent_name="pro_ctcae_survey_state",
            decision_type="pro_ctcae_survey",
        ) from exc
    external_response = chat_sync_response(payload, agent_response)
    return agent_response, external_response


def _resolve_food_selection_if_present(
    selection_store: FoodSelectionStateStore,
    *,
    payload: ChatSyncRequest,
    agent_payload: dict[str, Any],
) -> ResolvedFoodSelection | None:
    if payload.requested_return_type != "selection_box":
        return None
    context = agent_payload.get("context")
    if not isinstance(context, dict):
        return None
    structured = context.get("structured_response_context")
    if not isinstance(structured, dict):
        return None
    if structured.get("response_type") != "selection_box":
        return None
    originating_user_message_id = str(
        structured.get("originating_user_message_id") or ""
    ).strip()
    if not originating_user_message_id:
        return None
    return selection_store.resolve(
        patient_id=payload.patient_id,
        current_user_message_id=payload.message_id,
        originating_user_message_id=(
            originating_user_message_id
        ),
        submitted_value=payload.message,
    )


async def _submit_record_approval_if_present(
    *,
    payload: ChatSyncRequest,
    agent_payload: dict[str, Any],
) -> InternalApprovalDecision | None:
    if payload.requested_return_type != "selection_box":
        return None
    context = agent_payload.get("context")
    if not isinstance(context, dict):
        return None
    structured = context.get("structured_response_context")
    if not isinstance(structured, dict):
        return None
    if structured.get("response_type") != "selection_box":
        return None
    originating_user_message_id = str(
        structured.get("originating_user_message_id") or ""
    ).strip()
    source_message = structured.get("source_message")
    if (
        not originating_user_message_id
        or not isinstance(source_message, dict)
    ):
        return None
    return await asyncio.to_thread(
        _internal_approval_store().submit_response,
        patient_id=payload.patient_id,
        current_user_message_id=payload.message_id,
        current_source_chat_request_id=payload.request_id,
        originating_user_message_id=originating_user_message_id,
        submitted_value=payload.message,
        source_message=source_message,
    )


def _cancelled_approval_response(
    *,
    trace_id: str,
    action_name: str,
) -> AgentResponse:
    text = "요청을 취소했습니다. 변경된 내용은 없습니다."
    return AgentResponse(
        trace_id=trace_id,
        agent_name="internal_approval_state",
        prompt_version_id="internal_approval_v1",
        decision_type="record_approval_cancelled",
        structured_payload={
            "routing_mode": "record_approval_cancelled",
            "executed_by": "internal_approval_state",
            "final_answer_source": "deterministic_approval_state",
            "action_name": action_name,
            "chat_response": {
                "message_type": "text",
                "message": {
                    "message_title": None,
                    "text": text,
                    "tables": None,
                    "selections": None,
                    "inputs": None,
                },
            },
        },
        human_summary=text,
        requires_conversation_alert=False,
    )


async def _submit_pro_ctcae_response_if_present(
    survey_service: ProCtcaeSurveyService,
    *,
    payload: ChatSyncRequest,
    agent_payload: dict[str, Any],
) -> ProCtcaeSurveyTransition | None:
    if payload.requested_return_type != "selection_box":
        return None
    context = agent_payload.get("context")
    if not isinstance(context, dict):
        return None
    structured = context.get("structured_response_context")
    if not isinstance(structured, dict):
        return None
    if structured.get("response_type") != "selection_box":
        return None
    originating_user_message_id = str(
        structured.get("originating_user_message_id") or ""
    ).strip()
    if not originating_user_message_id:
        return None
    return await asyncio.to_thread(
        survey_service.submit_response,
        patient_id=payload.patient_id,
        current_user_message_id=payload.message_id,
        originating_user_message_id=originating_user_message_id,
        submitted_value=payload.message,
    )


async def _agent_response_for_survey_transition(
    survey_service: ProCtcaeSurveyService,
    *,
    orchestrator: AgentLangGraphNativeOrchestrator,
    payload: ChatSyncRequest,
    agent_payload: dict[str, Any],
    trace_id: str,
    transition: ProCtcaeSurveyTransition,
) -> AgentResponse:
    if transition.kind == "next_question":
        return pro_ctcae_question_response(
            transition,
            trace_id=trace_id,
        )
    completed = transition.completed_context
    if not isinstance(completed, dict):
        raise ProCtcaeSurveyError(
            "pro_ctcae_survey_completion_missing",
            retryable=True,
        )
    record_arguments = {
        "symptom_text": str(
            completed.get("symptom_text") or ""
        ),
        "symptom_onset_text": str(
            completed.get("symptom_onset_text") or ""
        ),
    }
    medication_name = str(
        completed.get("medication_name") or ""
    ).strip()
    if medication_name:
        record_arguments["medication_name"] = medication_name
    context = dict(agent_payload.get("context") or {})
    context["completed_pro_ctcae_survey"] = completed
    continuation_payload = {
        **agent_payload,
        "context": context,
    }
    response = (
        await orchestrator.multiturn_chat_agent.medication_agent.continue_with_tool_calls(
            trace_id,
            continuation_payload,
            tool_calls=[
                {
                    "id": (
                        f"survey_{transition.survey_id}_approval"
                    ),
                    "name": REQUEST_RECORD_APPROVAL,
                    "arguments": {
                        "action_name": (
                            CREATE_MEDICATION_SIDE_EFFECT_RECORD
                        ),
                        "record_arguments": record_arguments,
                    },
                }
            ],
        )
    )
    proposal = response.structured_payload.get(
        "mutation_confirmation"
    )
    if isinstance(proposal, dict) and proposal:
        await asyncio.to_thread(
            survey_service.mark_approval_pending,
            patient_id=payload.patient_id,
            survey_id=transition.survey_id,
        )
    return response


@router.post(
    SYNC_CHAT_PATH,
    response_class=StreamingResponse,
    responses={
        200: {
            "model": ChatStreamEvent,
            "description": (
                "LF-delimited ChatStreamEvent objects ending with exactly "
                "one completed or error event."
            ),
            "content": {
                NDJSON_MEDIA_TYPE: {
                    "schema": {
                        "type": "string",
                        "contentMediaType": NDJSON_MEDIA_TYPE,
                        "description": (
                            "One JSON object per LF-terminated line."
                        ),
                        "x-ndjson-item-schema": {
                            "$ref": "#/components/schemas/ChatStreamEvent"
                        },
                        "x-ndjson-framing": "one-json-object-per-lf-line",
                    }
                }
            },
        },
        **SYNC_CHAT_RESPONSES,
    },
    dependencies=[Depends(require_agent_sync_bearer_token)],
)
async def sync_chat(
    payload: ChatSyncRequest,
    accept: Annotated[str, Header(alias="Accept")],
) -> Response:
    settings = get_settings()
    internal_trace_id = str(uuid4())
    trace_store = _trace_store()
    if not _accepts_ndjson(accept):
        body = chat_error(
            "INVALID_REQUEST",
            "Request schema or required field is invalid.",
            request_id=payload.request_id,
            retryable=False,
            details={"header": "Accept"},
        ).model_dump(mode="json")
        try:
            trace_store.ensure_chat_started(
                payload,
                trace_id=internal_trace_id,
                api_path=SYNC_CHAT_PATH,
            )
            trace_store.fail_chat(
                trace_id=internal_trace_id,
                error_code="INVALID_REQUEST",
                error_message=(
                    "Request schema or required field is invalid."
                ),
                retryable=False,
            )
        except Exception as exc:
            trace_logging.log_info(
                "agent_trace_persistence_failed",
                request_id=payload.request_id,
                trace_id=internal_trace_id,
                error=safe_exception_summary(exc, limit=300),
            )
        return JSONResponse(status_code=400, content=body)

    gate = _sync_request_gate()
    trace_logging.log_info(
        "agent_api_call",
        path=SYNC_CHAT_PATH,
        mode="sync",
        task_type="multiturn_chat",
        request_id=payload.request_id,
        patient_id=payload.patient_id,
        message_id=payload.message_id,
    )
    try:
        begin_result = gate.begin(payload)
    except SyncRequestGateError as exc:
        headers = None
        if isinstance(
            exc,
            (RequestInProgressError, PatientThreadBusyError),
        ):
            headers = {
                "Retry-After": str(
                    max(1, settings.agent_sync_retry_after_seconds)
                )
            }
        body = chat_error(
            exc.code,
            exc.message,
            request_id=payload.request_id,
            retryable=exc.retryable,
            details=exc.details,
        ).model_dump(mode="json")
        try:
            trace_store.ensure_chat_started(
                payload,
                trace_id=internal_trace_id,
                api_path=SYNC_CHAT_PATH,
            )
            trace_store.fail_chat(
                trace_id=internal_trace_id,
                error_code=exc.code,
                error_message=exc.message,
                retryable=exc.retryable,
            )
        except Exception as trace_exc:
            trace_logging.log_info(
                "agent_trace_persistence_failed",
                request_id=payload.request_id,
                trace_id=internal_trace_id,
                error=safe_exception_summary(
                    trace_exc,
                    limit=300,
                ),
            )
        return JSONResponse(
            status_code=409,
            content=body,
            headers=headers,
        )

    if isinstance(begin_result, StoredHttpResponse):
        return _replay_chat_response(payload, begin_result)
    claim = begin_result

    backend_context: dict[str, Any] = {}
    backend_queries = _backend_query_tools()
    if backend_queries is None:
        trace_logging.log_info(
            "agent_backend_read_unavailable",
            request_id=payload.request_id,
            reason="backend_query_tools_not_configured",
        )
        return _final_chat_error(
            gate,
            trace_store,
            payload,
            claim=claim,
            code="BACKEND_DB_UNAVAILABLE",
            message=(
                "The read-only Backend DB connection is unavailable or "
                "incompatible."
            ),
            trace_id=internal_trace_id,
            status_code=503,
            retryable=True,
        )
    try:
        backend_context = await asyncio.to_thread(
            backend_queries.validate_chat_message,
            payload,
        )
        backend_context[
            "patient_context_snapshot"
        ] = await asyncio.to_thread(
            backend_queries.patient_context_snapshot,
            patient_id=payload.patient_id,
            as_of=payload.message_at,
        )
    except BackendChatMessageNotFound:
        return _final_chat_error(
            gate,
            trace_store,
            payload,
            claim=claim,
            code="BACKEND_MESSAGE_NOT_FOUND",
            message="The Backend user message could not be verified.",
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
            claim=claim,
            code="BACKEND_DB_UNAVAILABLE",
            message=(
                "The read-only Backend DB connection is unavailable or "
                "incompatible."
            ),
            trace_id=internal_trace_id,
            status_code=503,
            retryable=True,
        )

    try:
        gate.bind_trace(
            payload,
            internal_trace_id,
            claim=claim,
        )
        trace_store.start_chat(
            payload,
            trace_id=internal_trace_id,
            api_path=SYNC_CHAT_PATH,
        )
        orchestrator = _orchestrator()
    except Exception as exc:
        trace_logging.log_info(
            "agent_sync_chat_preflight_failed",
            request_id=payload.request_id,
            error=safe_exception_summary(exc, limit=300),
        )
        return _final_chat_error(
            gate,
            trace_store,
            payload,
            claim=claim,
            code="AI_PROCESSING_ERROR",
            message=(
                "An internal AI Server processing error occurred."
            ),
            trace_id=internal_trace_id,
            status_code=500,
            retryable=True,
        )

    return _stream_chat_response(
        gate=gate,
        trace_store=trace_store,
        orchestrator=orchestrator,
        payload=payload,
        claim=claim,
        agent_payload=agent_chat_payload(
            payload,
            backend_context=backend_context,
        ),
        trace_id=internal_trace_id,
        timeout_seconds=settings.agent_sync_chat_timeout_seconds,
    )


def _stream_chat_response(
    *,
    gate: SyncRequestGate,
    trace_store: AgentTraceStore,
    orchestrator: AgentLangGraphNativeOrchestrator,
    payload: ChatSyncRequest,
    claim: SyncRequestClaim,
    agent_payload: dict[str, Any],
    trace_id: str,
    timeout_seconds: float,
) -> StreamingResponse:
    queue: asyncio.Queue[ChatStreamEvent] = asyncio.Queue()
    client_connected = True
    public_text_published = False
    published_events: list[ChatStreamEvent] = []

    async def publish_text(text: str) -> None:
        nonlocal public_text_published
        if not text:
            return
        public_text_published = True
        event = ChatStreamEvent(
            request_id=payload.request_id,
            message_id=payload.message_id,
            sequence=0,
            status="streaming",
            message_type="text",
            delta=text,
            message=None,
            error=None,
            event_at=_event_time(),
        )
        published_events.append(event)
        if client_connected:
            await queue.put(event)

    async def run_chat() -> None:
        model_call_observations: list[dict[str, Any]] = []
        try:
            with (
                capture_model_calls(model_call_observations),
                publish_agent_text_with(publish_text),
            ):
                agent_response, external_response = (
                    await asyncio.wait_for(
                        _invoke_sync_chat_contract(
                            orchestrator=orchestrator,
                            agent_payload=agent_payload,
                            payload=payload,
                            trace_id=trace_id,
                        ),
                        timeout=timeout_seconds,
                    )
                )
            agent_response = response_with_model_calls(
                agent_response,
                model_call_observations,
            )
            if not public_text_published and external_response.message.text:
                # The normal Agent loop now owns the final answer. Publish that
                # single authoritative text before the completed response/card.
                await publish_text(external_response.message.text)
            terminal = ChatStreamEvent(
                request_id=payload.request_id,
                message_id=payload.message_id,
                sequence=0,
                status="completed",
                message_type=external_response.message_type,
                delta=None,
                message=external_response.message,
                error=None,
                event_at=external_response.message_at,
            )
            try:
                trace_store.complete_chat(
                    payload,
                    agent_response,
                    model_call_observations=(
                        model_call_observations
                    ),
                )
                stored_events = [
                    event.model_copy(
                        update={"sequence": sequence}
                    ).model_dump(mode="json")
                    for sequence, event in enumerate(
                        [*published_events, terminal]
                    )
                ]
                gate.complete(
                    payload,
                    claim=claim,
                    status_code=200,
                    body={
                        STORED_STREAM_EVENTS_KEY: stored_events,
                    },
                    trace_id=agent_response.trace_id,
                )
            except Exception as exc:
                trace_logging.log_info(
                    "agent_sync_chat_completion_persistence_failed",
                    request_id=payload.request_id,
                    trace_id=trace_id,
                    error=safe_exception_summary(exc, limit=300),
                )
                terminal = _stream_error_event(
                    payload,
                    code="AI_PROCESSING_ERROR",
                    message=(
                        "An internal AI Server processing error occurred."
                    ),
                    retryable=True,
                )
                _persist_stream_failure(
                    gate,
                    trace_store,
                    payload,
                    claim=claim,
                    terminal=terminal,
                    trace_id=trace_id,
                    model_call_observations=model_call_observations,
                )
        except TimeoutError as exc:
            public_error = public_processing_error(exc)
            terminal = _stream_error_event(
                payload,
                code=public_error.code,
                message=public_error.message,
                retryable=public_error.retryable,
            )
            _persist_stream_failure(
                gate,
                trace_store,
                payload,
                claim=claim,
                terminal=terminal,
                trace_id=trace_id,
                model_call_observations=model_call_observations,
            )
        except AgentExecutionError as exc:
            public_error = public_processing_error(exc)
            trace_logging.log_info(
                "agent_sync_chat_failed",
                request_id=payload.request_id,
                trace_id=exc.trace_id,
                error_type=exc.error_type,
                error=safe_exception_summary(exc, limit=300),
            )
            terminal = _stream_error_event(
                payload,
                code=public_error.code,
                message=public_error.message,
                retryable=public_error.retryable,
            )
            _persist_stream_failure(
                gate,
                trace_store,
                payload,
                claim=claim,
                terminal=terminal,
                trace_id=trace_id,
                model_call_observations=model_call_observations,
            )
        except Exception as exc:
            trace_logging.log_info(
                "agent_sync_chat_failed",
                request_id=payload.request_id,
                trace_id=trace_id,
                error=safe_exception_summary(exc, limit=300),
            )
            terminal = _stream_error_event(
                payload,
                code="AI_PROCESSING_ERROR",
                message=(
                    "An internal AI Server processing error occurred."
                ),
                retryable=True,
            )
            _persist_stream_failure(
                gate,
                trace_store,
                payload,
                claim=claim,
                terminal=terminal,
                trace_id=trace_id,
                model_call_observations=model_call_observations,
            )
        if client_connected:
            await queue.put(terminal)

    async def event_stream() -> AsyncIterator[str]:
        nonlocal client_connected
        worker = asyncio.create_task(run_chat())
        sequence = 0
        try:
            while True:
                event = await queue.get()
                outbound = event.model_copy(
                    update={"sequence": sequence}
                )
                sequence += 1
                yield _ndjson_line(outbound)
                if outbound.status in {"completed", "error"}:
                    await worker
                    return
        except asyncio.CancelledError:
            client_connected = False
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            raise
        finally:
            client_connected = False
            if worker.done() and not worker.cancelled():
                worker.exception()

    return _ndjson_streaming_response(event_stream())


def _persist_stream_failure(
    gate: SyncRequestGate,
    trace_store: AgentTraceStore,
    payload: ChatSyncRequest,
    *,
    claim: SyncRequestClaim,
    terminal: ChatStreamEvent,
    trace_id: str,
    model_call_observations: list[dict[str, Any]] | None = None,
) -> None:
    error = terminal.error
    if error is None:
        raise ValueError("stream_failure_requires_error")
    try:
        trace_store.record_model_calls(
            trace_id=trace_id,
            observations=model_call_observations or [],
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_model_observation_persistence_failed",
            request_id=payload.request_id,
            trace_id=trace_id,
            error=safe_exception_summary(exc, limit=300),
        )
    try:
        trace_store.fail_chat(
            trace_id=trace_id,
            error_code=error.code,
            error_message=error.message,
            retryable=error.retryable,
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_trace_persistence_failed",
            request_id=payload.request_id,
            trace_id=trace_id,
            error=safe_exception_summary(exc, limit=300),
        )
    try:
        gate.fail(
            payload,
            claim=claim,
            status_code=200,
            body=terminal.model_copy(
                update={"sequence": 0}
            ).model_dump(mode="json"),
            trace_id=trace_id,
            error_code=error.code,
            retryable=error.retryable,
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_sync_request_failure_persistence_failed",
            request_id=payload.request_id,
            trace_id=trace_id,
            error=safe_exception_summary(exc, limit=300),
        )


def _stream_error_event(
    payload: ChatSyncRequest,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> ChatStreamEvent:
    return ChatStreamEvent(
        request_id=payload.request_id,
        message_id=payload.message_id,
        sequence=0,
        status="error",
        message_type=None,
        delta=None,
        message=None,
        error=ChatContractError(
            code=code,
            message=message,
            retryable=retryable,
            details=None,
        ),
        event_at=_event_time(),
    )


def _replay_chat_response(
    payload: ChatSyncRequest,
    replay: StoredHttpResponse,
) -> Response:
    if replay.status_code != 200:
        return JSONResponse(
            status_code=replay.status_code,
            content=replay.body,
        )
    events = _stored_stream_events(payload, replay.body)

    async def replay_stream() -> AsyncIterator[str]:
        for event in events:
            yield _ndjson_line(event)

    return _ndjson_streaming_response(replay_stream())


def _stored_stream_events(
    payload: ChatSyncRequest,
    body: dict[str, Any],
) -> list[ChatStreamEvent]:
    raw_events = body.get(STORED_STREAM_EVENTS_KEY)
    if raw_events is None:
        return [
            _stored_terminal_event(payload, body).model_copy(
                update={"sequence": 0}
            )
        ]
    if not isinstance(raw_events, list) or not raw_events:
        raise RuntimeError("stored_chat_stream_events_invalid")

    events = [
        ChatStreamEvent.model_validate(raw_event)
        for raw_event in raw_events
    ]
    for sequence, event in enumerate(events):
        if (
            event.request_id != payload.request_id
            or event.message_id != payload.message_id
            or event.sequence != sequence
        ):
            raise RuntimeError("stored_chat_response_correlation_mismatch")
        if sequence < len(events) - 1 and event.status in {
            "completed",
            "error",
        }:
            raise RuntimeError("stored_chat_stream_terminal_not_last")
    if events[-1].status not in {"completed", "error"}:
        raise RuntimeError("stored_chat_stream_terminal_missing")
    return events


def _stored_terminal_event(
    payload: ChatSyncRequest,
    body: dict[str, Any],
) -> ChatStreamEvent:
    terminal = ChatStreamEvent.model_validate(body)
    if (
        terminal.request_id != payload.request_id
        or terminal.message_id != payload.message_id
    ):
        raise RuntimeError("stored_chat_response_correlation_mismatch")
    return terminal


def _accepts_ndjson(value: str | None) -> bool:
    accepted = {
        item.split(";", 1)[0].strip().lower()
        for item in str(value or "").split(",")
        if item.strip()
    }
    return NDJSON_MEDIA_TYPE in accepted


def _ndjson_streaming_response(
    content: AsyncIterator[str],
) -> StreamingResponse:
    return StreamingResponse(
        content,
        status_code=200,
        headers={
            "Content-Type": (
                f"{NDJSON_MEDIA_TYPE}; charset=utf-8"
            ),
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _ndjson_line(event: ChatStreamEvent) -> str:
    return (
        json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _event_time() -> datetime:
    return datetime.now(UTC)


def _final_chat_error(
    gate: SyncRequestGate,
    trace_store: AgentTraceStore,
    payload: ChatSyncRequest,
    *,
    claim: SyncRequestClaim,
    code: str,
    message: str,
    trace_id: str = "",
    status_code: int = 500,
    retryable: bool = False,
) -> JSONResponse:
    body = chat_error(
        code,
        message,
        request_id=payload.request_id,
        retryable=retryable,
        details=None,
    ).model_dump(mode="json")
    try:
        trace_store.ensure_chat_started(
            payload,
            trace_id=trace_id,
            api_path=SYNC_CHAT_PATH,
        )
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
        claim=claim,
        status_code=status_code,
        body=body,
        trace_id=trace_id,
        error_code=code,
        retryable=retryable,
    )
    return JSONResponse(status_code=status_code, content=body)
