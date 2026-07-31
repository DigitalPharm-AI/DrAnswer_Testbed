from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.public_ids import AssistantMessageId, DoseEventId, new_public_id
from shared.settings import get_settings
from system_app.contracts_v13 import BackendChatRequest
from system_app.db import get_session
from system_app.models import ChatMessage, DoseEvent
from system_app.runtime import SystemRuntime
from system_app.services.agent_client import AgentServiceError
from system_app.services.backend_chat_service import (
    BackendChatConflict,
    PendingChatResponseError,
    build_agent_chat_request,
    call_agent_and_persist_response,
    existing_backend_chat_response,
    mark_user_message_failed,
    persist_user_message,
)
from system_app.services.backend_v13_service import (
    BackendRequestConflict,
    BackendRequestGate,
)
from system_app.services.clock_service import ensure_clock
from system_app.services.chat_stream import publish_ui_text_with
from system_app.services.dose_event_service import (
    mark_dose_taken_command,
    set_clock_state,
)
from system_app.services.missed_dose_conversation_service import (
    active_missed_dose_conversation_alert,
    missed_dose_conversation_alert_for_message,
)
from system_app.services.missed_dose_reply_understanding import (
    complete_missed_dose_reply,
    missed_dose_reply_request_metadata,
)
from system_app.services.patient_profile_service import can_run_simulation
from system_app.services.ui_medication_scenario_service import (
    apply_test_medication_scenario,
    get_test_medication_scenario,
    list_test_medication_scenarios,
)
from system_app.services.ui_policy_service import (
    UI_POLICY_KEYS,
    set_ui_policy,
    ui_policy_state,
)
from system_app.services.ui_status_service import collect_ui_service_status
from system_app.services.ui_testbed_reset_service import (
    reset_testbed_state,
    testbed_reset_is_busy,
    testbed_reset_is_enabled,
)
from system_app.services.ui_view_service import (
    NotificationCursorNotFound,
    acknowledge_all_notifications,
    chat_history_page,
    clock_view,
    dashboard_view,
    dose_view_by_public_id,
    notification_detail,
    notification_list,
    nutrition_dashboard_ui_view,
)
from system_app.services.timeline_service import (
    ensure_chat_message_for_conversation_alert,
)
from system_app.ui_contracts import (
    AdvanceClockRequest,
    ApplyMedicationScenarioRequest,
    EmptyUiRequest,
    PlayClockRequest,
    ResetTestbedRequest,
    UiSystemStatusResponse,
    UiTestbedResetResponse,
    UiChatHistoryResponse,
    UiChatRequest,
    UiChatSyncResponse,
    UiClockResponse,
    UiDashboardResponse,
    UiDoseResponse,
    UiErrorResponse,
    UiMedicationScenarioApplyResponse,
    UiMedicationScenarioListResponse,
    UiNotificationAcknowledgementResponse,
    UiNotificationDetailResponse,
    UiNotificationListResponse,
    UiNutritionResponse,
    UiPoliciesResponse,
    UpdateUiPolicyRequest,
)

UI_PREFIX = "/api/ui/v1"
APPLY_SCENARIO_PATH = f"{UI_PREFIX}/medication-scenarios/apply"
ADVANCE_CLOCK_PATH = f"{UI_PREFIX}/clock/advance"
TESTBED_RESET_PATH = f"{UI_PREFIX}/testbed/reset"
_SEOUL = ZoneInfo("Asia/Seoul")
_STREAM_HEARTBEAT_SECONDS = 15.0
_STREAM_QUEUE_MAX_EVENTS = 256
_UI_ERROR_DESCRIPTIONS = {
    403: "The UI action is not available in this environment.",
    404: "The requested UI resource was not found.",
    409: "The UI request conflicts with the current or idempotent state.",
    422: "The UI request schema, path, or query value is invalid.",
    500: "The Backend could not complete the UI request.",
    502: "The AI Server returned an invalid response or was unavailable.",
}


class UiResponseContractError(RuntimeError):
    pass


def _ui_error_responses(*status_codes: int) -> dict[int, dict[str, Any]]:
    return {
        status_code: {
            "model": UiErrorResponse,
            "description": _UI_ERROR_DESCRIPTIONS[status_code],
        }
        for status_code in status_codes
    }


def create_ui_api_router(
    get_runtime: Callable[[], SystemRuntime],
) -> APIRouter:
    router = APIRouter(prefix=UI_PREFIX)

    @router.get(
        "/dashboard",
        response_model=UiDashboardResponse,
        responses=_ui_error_responses(422, 500),
    )
    async def get_dashboard(
        date_value: date | None = Query(default=None, alias="date"),
        session: Session = Depends(get_session),
    ):
        return ui_success(
            UiDashboardResponse,
            dashboard_view(session, target_date=date_value),
        )

    @router.get(
        "/status",
        response_model=UiSystemStatusResponse,
        responses=_ui_error_responses(500),
    )
    async def get_system_status(
        _request: Request,
        session: Session = Depends(get_session),
    ):
        return ui_success(
            UiSystemStatusResponse,
            await collect_ui_service_status(
                session,
                request_round_trip=True,
            )
        )

    @router.get(
        "/medication-scenarios",
        response_model=UiMedicationScenarioListResponse,
        responses=_ui_error_responses(422, 500),
    )
    async def get_medication_scenarios(
        session: Session = Depends(get_session),
    ):
        return ui_success(
            UiMedicationScenarioListResponse,
            {"scenarios": list_test_medication_scenarios(session)}
        )

    @router.post(
        "/medication-scenarios/apply",
        response_model=UiMedicationScenarioApplyResponse,
        responses=_ui_error_responses(404, 409, 422, 500),
    )
    async def apply_medication_scenario(
        payload: ApplyMedicationScenarioRequest,
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        gate = BackendRequestGate()
        with runtime.write_lock:
            try:
                replay = gate.begin(
                    session,
                    api_path=APPLY_SCENARIO_PATH,
                    request_id=payload.request_id,
                    payload=payload,
                )
                if replay is not None:
                    session.rollback()
                    return ui_contract_response(
                        UiMedicationScenarioApplyResponse,
                        status_code=replay.status_code,
                        body=replay.body,
                    )
                if get_test_medication_scenario(
                    session,
                    payload.scenario_id,
                ) is None:
                    body = ui_error_body(
                        "MEDICATION_SCENARIO_NOT_FOUND",
                        "The selected medication scenario was not found.",
                    )
                    gate.complete(
                        session,
                        api_path=APPLY_SCENARIO_PATH,
                        request_id=payload.request_id,
                        status_code=404,
                        body=body,
                        error_code="MEDICATION_SCENARIO_NOT_FOUND",
                    )
                    session.commit()
                    return JSONResponse(status_code=404, content=body)
                data = apply_test_medication_scenario(
                    session,
                    scenario_id=payload.scenario_id,
                    schedule_date=payload.schedule_date,
                )
                body = ui_success_body(
                    UiMedicationScenarioApplyResponse,
                    data,
                )
                gate.complete(
                    session,
                    api_path=APPLY_SCENARIO_PATH,
                    request_id=payload.request_id,
                    status_code=200,
                    body=body,
                )
                session.commit()
                return JSONResponse(status_code=200, content=body)
            except BackendRequestConflict as exc:
                session.rollback()
                return JSONResponse(
                    status_code=409,
                    content=ui_error_body(exc.code, exc.message),
                )
            except ValueError as exc:
                session.rollback()
                return JSONResponse(
                    status_code=422,
                    content=ui_error_body(
                        "MEDICATION_SCENARIO_INVALID",
                        str(exc),
                    ),
                )
            except Exception:
                session.rollback()
                return JSONResponse(
                    status_code=500,
                    content=ui_error_body(
                        "BACKEND_PROCESSING_ERROR",
                        "The medication scenario could not be applied.",
                        retryable=True,
                    ),
                )

    @router.post(
        "/testbed/reset",
        response_model=UiTestbedResetResponse,
        responses=_ui_error_responses(403, 409, 422, 500),
    )
    async def reset_testbed(
        payload: ResetTestbedRequest,
        session: Session = Depends(get_session),
    ):
        if not testbed_reset_is_enabled():
            return JSONResponse(
                status_code=403,
                content=ui_error_body(
                    "TESTBED_RESET_DISABLED",
                    "Testbed reset is disabled in this environment.",
                ),
            )

        runtime = get_runtime()
        gate = BackendRequestGate()
        with runtime.write_lock:
            if testbed_reset_is_busy(session):
                return JSONResponse(
                    status_code=409,
                    content=ui_error_body(
                        "TESTBED_BUSY",
                        "An AI or Backend request is still being processed.",
                        retryable=True,
                    ),
                )
            try:
                replay = gate.begin(
                    session,
                    api_path=TESTBED_RESET_PATH,
                    request_id=payload.request_id,
                    payload=payload,
                )
                if replay is not None:
                    session.rollback()
                    return ui_contract_response(
                        UiTestbedResetResponse,
                        status_code=replay.status_code,
                        body=replay.body,
                    )
                reset_testbed_state(session)
                body = ui_success_body(
                    UiTestbedResetResponse,
                    {
                        "request_id": payload.request_id,
                        "reset_applied": True,
                        "reset_at": datetime.now(UTC).isoformat(),
                    }
                )
                gate.complete(
                    session,
                    api_path=TESTBED_RESET_PATH,
                    request_id=payload.request_id,
                    status_code=200,
                    body=body,
                )
                session.commit()
                return JSONResponse(status_code=200, content=body)
            except BackendRequestConflict as exc:
                session.rollback()
                return JSONResponse(
                    status_code=409,
                    content=ui_error_body(exc.code, exc.message),
                )
            except Exception:
                session.rollback()
                return JSONResponse(
                    status_code=500,
                    content=ui_error_body(
                        "BACKEND_PROCESSING_ERROR",
                        "The testbed could not be reset.",
                        retryable=True,
                    ),
                )

    @router.post(
        "/clock/advance",
        response_model=UiClockResponse,
        responses=_ui_error_responses(409, 422, 500),
    )
    async def advance_clock(
        payload: AdvanceClockRequest,
        session: Session = Depends(get_session),
    ):
        gate = BackendRequestGate()
        with get_runtime().write_lock:
            _require_simulation_ready(session)
            try:
                replay = gate.begin(
                    session,
                    api_path=ADVANCE_CLOCK_PATH,
                    request_id=payload.request_id,
                    payload=payload,
                )
                if replay is not None:
                    session.rollback()
                    return ui_contract_response(
                        UiClockResponse,
                        status_code=replay.status_code,
                        body=replay.body,
                    )
                await set_clock_state(
                    session,
                    advance_minutes=payload.minutes,
                    commit=False,
                )
                body = ui_success_body(
                    UiClockResponse,
                    {"clock": clock_view(session)},
                )
                gate.complete(
                    session,
                    api_path=ADVANCE_CLOCK_PATH,
                    request_id=payload.request_id,
                    status_code=200,
                    body=body,
                )
                session.commit()
                return JSONResponse(status_code=200, content=body)
            except BackendRequestConflict as exc:
                session.rollback()
                return JSONResponse(
                    status_code=409,
                    content=ui_error_body(exc.code, exc.message),
                )
            except Exception:
                session.rollback()
                return JSONResponse(
                    status_code=500,
                    content=ui_error_body(
                        "BACKEND_PROCESSING_ERROR",
                        "The simulation clock could not be advanced.",
                        retryable=True,
                    ),
                )

    @router.post(
        "/clock/play",
        response_model=UiClockResponse,
        responses=_ui_error_responses(409, 422, 500),
    )
    async def play_clock(
        payload: PlayClockRequest,
        session: Session = Depends(get_session),
    ):
        with get_runtime().write_lock:
            _require_simulation_ready(session)
            await set_clock_state(
                session,
                is_running=True,
                speed_multiplier=payload.speed_multiplier,
            )
        return ui_success(
            UiClockResponse,
            {"clock": clock_view(session)},
        )

    @router.post(
        "/clock/pause",
        response_model=UiClockResponse,
        responses=_ui_error_responses(422, 500),
    )
    async def pause_clock(
        _payload: EmptyUiRequest | None = None,
        session: Session = Depends(get_session),
    ):
        with get_runtime().write_lock:
            await set_clock_state(
                session,
                is_running=False,
                speed_multiplier=0,
            )
        return ui_success(
            UiClockResponse,
            {"clock": clock_view(session)},
        )

    @router.post(
        "/doses/{dose_event_id}/take",
        response_model=UiDoseResponse,
        responses=_ui_error_responses(404, 422, 500),
    )
    async def take_dose(
        dose_event_id: DoseEventId,
        _payload: EmptyUiRequest | None = None,
        session: Session = Depends(get_session),
    ):
        settings = get_settings()
        with get_runtime().write_lock:
            event = session.scalar(
                select(DoseEvent).where(
                    DoseEvent.public_id == dose_event_id,
                    DoseEvent.patient_id == settings.patient_id,
                )
            )
            if event is None:
                raise _ui_http_error(
                    404,
                    "DOSE_EVENT_NOT_FOUND",
                    "The dose event was not found.",
                )
            mark_dose_taken_command(session, event.id)
            session.commit()
            result = dose_view_by_public_id(session, dose_event_id)
        return ui_success(UiDoseResponse, {"dose": result})

    @router.get(
        "/nutrition",
        response_model=UiNutritionResponse,
        responses=_ui_error_responses(422, 500),
    )
    async def get_nutrition(
        date_value: date | None = Query(default=None, alias="date"),
        session: Session = Depends(get_session),
    ):
        return ui_success(
            UiNutritionResponse,
            nutrition_dashboard_ui_view(
                session,
                patient_id=get_settings().patient_id,
                target_date=date_value,
            )
        )

    @router.get(
        "/policies",
        response_model=UiPoliciesResponse,
        responses=_ui_error_responses(422, 500),
    )
    async def get_policies(
        session: Session = Depends(get_session),
    ):
        return ui_success(
            UiPoliciesResponse,
            {
                "policies": ui_policy_state(
                    session,
                    patient_id=get_settings().patient_id,
                )
            }
        )

    @router.put(
        "/policies/{policy_key}",
        response_model=UiPoliciesResponse,
        responses=_ui_error_responses(404, 422, 500),
    )
    async def update_policy(
        policy_key: str,
        payload: UpdateUiPolicyRequest,
        session: Session = Depends(get_session),
    ):
        if policy_key not in UI_POLICY_KEYS:
            raise _ui_http_error(
                404,
                "UI_POLICY_NOT_FOUND",
                "The UI policy was not found.",
            )
        with get_runtime().write_lock:
            policies = set_ui_policy(
                session,
                policy_key,
                payload.enabled,
                patient_id=get_settings().patient_id,
            )
            session.commit()
        return ui_success(UiPoliciesResponse, {"policies": policies})

    @router.get(
        "/notifications",
        response_model=UiNotificationListResponse,
        responses=_ui_error_responses(422, 500),
    )
    async def get_notifications(
        after_id: str | None = Query(
            default=None,
            pattern=r"^notif_[0-9a-f]{32}$",
        ),
        limit: int = Query(default=50, ge=1, le=100),
        session: Session = Depends(get_session),
    ):
        try:
            return ui_success(
                UiNotificationListResponse,
                notification_list(
                    session,
                    after_id=after_id,
                    limit=limit,
                )
            )
        except NotificationCursorNotFound as exc:
            raise _ui_http_error(
                422,
                "NOTIFICATION_CURSOR_INVALID",
                "The notification cursor is invalid or no longer available.",
            ) from exc

    @router.get(
        "/notifications/{notification_id}",
        response_model=UiNotificationDetailResponse,
        responses=_ui_error_responses(404, 422, 500),
    )
    async def get_notification(
        notification_id: str = Path(
            pattern=r"^notif_[0-9a-f]{32}$",
        ),
        session: Session = Depends(get_session),
    ):
        result = notification_detail(session, notification_id)
        if result is None:
            raise _ui_http_error(
                404,
                "NOTIFICATION_NOT_FOUND",
                "The notification was not found.",
            )
        return ui_success(
            UiNotificationDetailResponse,
            {"notification": result},
        )

    @router.post(
        "/notifications/ack-all",
        response_model=UiNotificationAcknowledgementResponse,
        responses=_ui_error_responses(422, 500),
    )
    async def acknowledge_notifications(
        _payload: EmptyUiRequest | None = None,
        session: Session = Depends(get_session),
    ):
        with get_runtime().write_lock:
            count = acknowledge_all_notifications(session)
            session.commit()
        return ui_success(
            UiNotificationAcknowledgementResponse,
            {"acknowledged_count": count},
        )

    @router.get(
        "/chat/history",
        response_model=UiChatHistoryResponse,
        responses=_ui_error_responses(422, 500),
    )
    async def get_chat_history(
        before_date: date | None = None,
        limit_days: int = Query(default=1, ge=1, le=7),
        limit_turns: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(get_session),
    ):
        return ui_success(
            UiChatHistoryResponse,
            chat_history_page(
                session,
                before_date=before_date,
                limit_days=limit_days,
                limit_turns=limit_turns,
            )
        )

    @router.post(
        "/chat/sync",
        response_model=UiChatSyncResponse,
        responses=_ui_error_responses(409, 422, 500, 502),
    )
    async def sync_chat(
        payload: UiChatRequest,
        session: Session = Depends(get_session),
    ):
        return await _sync_ui_chat(
            get_runtime(),
            session,
            payload,
        )

    @router.post(
        "/chat/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Synchronous final-answer stream.",
                "content": {"text/event-stream": {}},
            }
        },
    )
    async def stream_chat(
        payload: UiChatRequest,
        session: Session = Depends(get_session),
    ):
        if payload.request_id is None:
            payload = payload.model_copy(
                update={"request_id": new_public_id("request")}
            )
        return _stream_ui_chat(
            get_runtime(),
            session,
            payload,
        )

    return router


def _stream_ui_chat(
    runtime: SystemRuntime,
    session: Session,
    payload: UiChatRequest,
) -> StreamingResponse:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(
        maxsize=_STREAM_QUEUE_MAX_EVENTS
    )

    async def publish_text(text: str) -> None:
        await queue.put(("text_delta", text))

    async def run_chat() -> None:
        try:
            with publish_ui_text_with(publish_text):
                response = await _sync_ui_chat(runtime, session, payload)
            await queue.put(("response", response))
        except Exception:
            await queue.put(
                (
                    "fatal_error",
                    {
                        "request_id": payload.request_id,
                        "code": "BACKEND_CHAT_PROCESSING_ERROR",
                        "message": (
                            "AI 답변을 처리하지 못했습니다. "
                            "대체 응답은 생성하지 않았습니다."
                        ),
                        "retryable": True,
                        "details": None,
                    },
                )
            )

    async def event_stream():
        worker = asyncio.create_task(run_chat())
        worker.add_done_callback(_consume_stream_worker_exception)
        sequence = 0
        yield _ui_sse_frame(
            "start",
            {
                "request_id": payload.request_id,
                "status": "processing",
            },
        )
        try:
            while True:
                try:
                    event_type, event_payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=_STREAM_HEARTBEAT_SECONDS,
                    )
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event_type == "text_delta":
                    yield _ui_sse_frame(
                        "text_delta",
                        {
                            "request_id": payload.request_id,
                            "sequence": sequence,
                            "text": event_payload,
                        },
                    )
                    sequence += 1
                    continue
                if event_type == "fatal_error":
                    yield _ui_sse_frame("error", event_payload)
                    return

                response = event_payload
                body = _ui_json_response_body(response)
                if response.status_code == 200:
                    data = body.get("data")
                    if isinstance(data, dict):
                        yield _ui_sse_frame("completed", data)
                    else:
                        yield _ui_sse_frame(
                            "error",
                            {
                                "code": "BACKEND_STREAM_INVALID",
                                "message": "Backend 완료 응답을 확인할 수 없습니다.",
                                "retryable": False,
                                "details": None,
                            },
                        )
                else:
                    error = body.get("error")
                    error_body = (
                        error
                        if isinstance(error, dict)
                        else {
                            "code": "BACKEND_CHAT_PROCESSING_ERROR",
                            "message": (
                                "Backend가 채팅 요청을 처리하지 못했습니다."
                            ),
                            "retryable": response.status_code >= 500,
                            "details": None,
                        }
                    )
                    yield _ui_sse_frame(
                        "error",
                        {
                            **error_body,
                            "request_id": payload.request_id,
                        },
                    )
                return
        except asyncio.CancelledError:
            if not worker.done():
                try:
                    await asyncio.shield(worker)
                except Exception:
                    pass
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _sync_ui_chat(
    runtime: SystemRuntime,
    session: Session,
    payload: UiChatRequest,
):
    settings = get_settings()
    clock = ensure_clock(session)
    display_message_at = clock.current_time.replace(tzinfo=_SEOUL)
    backend_payload = BackendChatRequest(
        patient_id=settings.patient_id,
        message=payload.message,
        requested_return_type=payload.requested_return_type,
        source_message_id=(
            payload.source_message_id
            if payload.requested_return_type != "text"
            else None
        ),
        request_id=payload.request_id,
        message_at=display_message_at,
    )
    missed_dose_notification_id: int | None = None
    try:
        with runtime.write_lock:
            user_message, request_id, message_at = persist_user_message(
                session,
                backend_payload,
            )
            display_message_at = message_at
            user_metadata = parse_json_object(user_message.metadata_json)
            requested_source_message_id = payload.source_message_id
            if "ui_source_message_id" in user_metadata:
                if (
                    user_metadata.get("ui_source_message_id")
                    != requested_source_message_id
                ):
                    raise BackendChatConflict("IDEMPOTENCY_CONFLICT")
            else:
                user_metadata["ui_source_message_id"] = (
                    requested_source_message_id
                )
            existing_missed_dose_context = user_metadata.get(
                "missed_dose_reply"
            )
            if isinstance(existing_missed_dose_context, dict):
                try:
                    missed_dose_notification_id = int(
                        existing_missed_dose_context.get(
                            "conversation_alert_id"
                        )
                    )
                except (TypeError, ValueError):
                    missed_dose_notification_id = None

            missed_dose_source = None
            if requested_source_message_id:
                missed_dose_source = missed_dose_conversation_alert_for_message(
                    session,
                    requested_source_message_id,
                )
            else:
                notification = active_missed_dose_conversation_alert(
                    session,
                    include_acknowledged=True,
                )
                if notification is not None:
                    prompt_message = (
                        ensure_chat_message_for_conversation_alert(
                            session,
                            notification,
                        )
                    )
                    if prompt_message is not None:
                        missed_dose_source = (notification, prompt_message)

            if (
                missed_dose_source is not None
                and "missed_dose_reply" not in user_metadata
            ):
                notification, _prompt_message = missed_dose_source
                missed_dose_notification_id = notification.id
                user_metadata.update(
                    missed_dose_reply_request_metadata(
                        notification,
                    )
                )
            user_message.metadata_json = dump_json(user_metadata)
            session.flush()
            replay = existing_backend_chat_response(
                session,
                request_id=request_id,
            )
            agent_request = build_agent_chat_request(
                user_message=user_message,
                request_id=request_id,
                message_at=message_at,
                requested_return_type=payload.requested_return_type,
            )
            session.commit()
            if replay is not None:
                complete_missed_dose_reply(
                    session,
                    notification_id=missed_dose_notification_id,
                    assistant_message_id=replay.assistant_message_id,
                )
                assistant_sort_sequence = _set_display_message_at(
                    session,
                    assistant_public_id=replay.assistant_message_id,
                    display_message_at=display_message_at.isoformat(),
                )
                session.commit()
                return ui_success(
                    UiChatSyncResponse,
                    {
                        **replay.model_dump(mode="json"),
                        "user_sort_sequence": user_message.id,
                        "assistant_sort_sequence": assistant_sort_sequence,
                        "display_message_at": display_message_at.isoformat(),
                    }
                )
            user_message_id = user_message.id
    except BackendChatConflict:
        session.rollback()
        return JSONResponse(
            status_code=409,
            content=ui_error_body(
                "IDEMPOTENCY_CONFLICT",
                "The request_id was already used with a different chat body.",
            ),
        )
    except PendingChatResponseError as exc:
        session.rollback()
        return JSONResponse(
            status_code=409,
            content=ui_error_body(exc.code, exc.message),
        )
    except Exception:
        session.rollback()
        return JSONResponse(
            status_code=500,
            content=ui_error_body(
                "BACKEND_CHAT_PROCESSING_ERROR",
                "The chat request could not be persisted.",
                retryable=True,
            ),
        )
    try:
        response = await call_agent_and_persist_response(
            session,
            runtime.agent_client,
            request=agent_request,
        )
        with runtime.write_lock:
            complete_missed_dose_reply(
                session,
                notification_id=missed_dose_notification_id,
                assistant_message_id=response.assistant_message_id,
            )
            assistant_sort_sequence = _set_display_message_at(
                session,
                assistant_public_id=response.assistant_message_id,
                display_message_at=display_message_at.isoformat(),
            )
            session.commit()
        return ui_success(
            UiChatSyncResponse,
            {
                **response.model_dump(mode="json"),
                "user_sort_sequence": user_message_id,
                "assistant_sort_sequence": assistant_sort_sequence,
                "display_message_at": display_message_at.isoformat(),
            }
        )
    except AgentServiceError as exc:
        session.rollback()
        with runtime.write_lock:
            mark_user_message_failed(
                session,
                user_message_id=user_message_id,
                error_code=exc.error_type,
                retryable=exc.retryable,
            )
            session.commit()
        return JSONResponse(
            status_code=502,
            content=ui_error_body(
                exc.error_type,
                (
                    "AI Server 연결 또는 답변 생성에 실패했습니다. "
                    "대체 응답은 생성하지 않았습니다."
                ),
                retryable=exc.retryable,
            ),
        )
    except Exception:
        session.rollback()
        with runtime.write_lock:
            mark_user_message_failed(
                session,
                user_message_id=user_message_id,
                error_code="BACKEND_CHAT_PROCESSING_ERROR",
                retryable=True,
            )
            session.commit()
        return JSONResponse(
            status_code=500,
            content=ui_error_body(
                "BACKEND_CHAT_PROCESSING_ERROR",
                (
                    "AI 답변을 처리하지 못했습니다. "
                    "대체 응답은 생성하지 않았습니다."
                ),
                retryable=True,
            ),
        )


def _ui_sse_frame(event: str, data: dict[str, Any]) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _ui_json_response_body(response: JSONResponse) -> dict[str, Any]:
    try:
        value = json.loads(bytes(response.body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _consume_stream_worker_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    task.exception()


def _set_display_message_at(
    session: Session,
    *,
    assistant_public_id: AssistantMessageId,
    display_message_at: str,
) -> int:
    assistant = session.scalar(
        select(ChatMessage).where(
            ChatMessage.public_id == assistant_public_id,
            ChatMessage.patient_id == get_settings().patient_id,
            ChatMessage.role == "assistant",
        )
    )
    if assistant is None:
        raise RuntimeError("assistant_message_not_found")
    metadata = parse_json_object(assistant.metadata_json)
    metadata["display_message_at"] = display_message_at
    assistant.metadata_json = dump_json(metadata)
    parsed_display_at = datetime.fromisoformat(display_message_at)
    if (
        parsed_display_at.tzinfo is not None
        and parsed_display_at.utcoffset() is not None
    ):
        parsed_display_at = parsed_display_at.astimezone(UTC).replace(
            tzinfo=None
        )
    else:
        parsed_display_at = (
            parsed_display_at.replace(tzinfo=_SEOUL)
            .astimezone(UTC)
            .replace(tzinfo=None)
        )
    assistant.display_at = parsed_display_at
    session.flush()
    return assistant.id


def _require_simulation_ready(session: Session) -> None:
    if not can_run_simulation(session):
        raise _ui_http_error(
            409,
            "SIMULATION_NOT_READY",
            "A test medication scenario is required before running the simulation.",
        )


def _ui_http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "details": details,
        },
    )


def ui_success(
    response_model: type[BaseModel],
    data: Any,
) -> JSONResponse:
    return ui_contract_response(
        response_model,
        status_code=200,
        body={
            "success": True,
            "data": data,
            "error": None,
        },
    )


def ui_success_body(
    response_model: type[BaseModel],
    data: Any,
) -> dict[str, Any]:
    return _validated_ui_response_body(
        response_model,
        {
            "success": True,
            "data": data,
            "error": None,
        },
    )


def ui_contract_response(
    response_model: type[BaseModel],
    *,
    status_code: int,
    body: Any,
) -> JSONResponse:
    contract_model = (
        response_model if status_code == 200 else UiErrorResponse
    )
    content = _validated_ui_response_body(contract_model, body)
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(content),
    )


def _validated_ui_response_body(
    response_model: type[BaseModel],
    body: Any,
) -> dict[str, Any]:
    try:
        response = response_model.model_validate(body)
    except ValidationError as exc:
        raise UiResponseContractError(
            "ui_response_contract_validation_failed"
        ) from exc
    return response.model_dump(mode="json")


def ui_error_body(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details,
        },
    }
