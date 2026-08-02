from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.public_ids import DoseEventId
from shared.settings import get_settings
from system_app.db import get_session
from system_app.models import DoseEvent
from system_app.routes.ui_patient_routes import create_patient_ui_router
from system_app.routes.ui_responses import (
    ui_contract_response,
    ui_error_body,
    ui_error_responses,
    ui_http_error,
    ui_success,
    ui_success_body,
)
from system_app.runtime import SystemRuntime
from system_app.services.backend_v13_service import (
    BackendRequestConflict,
    BackendRequestGate,
)
from system_app.services.dose_event_service import (
    mark_dose_taken_command,
    set_clock_state,
)
from system_app.services.patient_profile_service import can_run_simulation
from system_app.services.ui_medication_scenario_service import (
    apply_test_medication_scenario,
    get_test_medication_scenario,
    list_test_medication_scenarios,
)
from system_app.services.ui_status_service import collect_ui_service_status
from system_app.services.ui_testbed_reset_service import (
    reset_testbed_state,
    testbed_reset_is_busy,
    testbed_reset_is_enabled,
)
from system_app.services.ui_view_service import (
    clock_view,
    dashboard_view,
    dose_view_by_public_id,
)
from system_app.ui_contracts import (
    AdvanceClockRequest,
    ApplyMedicationScenarioRequest,
    EmptyUiRequest,
    PlayClockRequest,
    ResetTestbedRequest,
    UiClockResponse,
    UiDashboardResponse,
    UiDoseResponse,
    UiMedicationScenarioApplyResponse,
    UiMedicationScenarioListResponse,
    UiSystemStatusResponse,
    UiTestbedResetResponse,
)

UI_PREFIX = "/api/ui/v1"
APPLY_SCENARIO_PATH = f"{UI_PREFIX}/medication-scenarios/apply"
ADVANCE_CLOCK_PATH = f"{UI_PREFIX}/clock/advance"
TESTBED_RESET_PATH = f"{UI_PREFIX}/testbed/reset"


def create_ui_api_router(
    get_runtime: Callable[[], SystemRuntime],
) -> APIRouter:
    router = APIRouter(prefix=UI_PREFIX)

    @router.get(
        "/dashboard",
        response_model=UiDashboardResponse,
        responses=ui_error_responses(422, 500),
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
        responses=ui_error_responses(500),
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
        responses=ui_error_responses(422, 500),
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
        responses=ui_error_responses(404, 409, 422, 500),
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
        responses=ui_error_responses(403, 409, 422, 500),
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
        responses=ui_error_responses(409, 422, 500),
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
        responses=ui_error_responses(409, 422, 500),
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
        responses=ui_error_responses(422, 500),
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
        responses=ui_error_responses(404, 422, 500),
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
                raise ui_http_error(
                    404,
                    "DOSE_EVENT_NOT_FOUND",
                    "The dose event was not found.",
                )
            mark_dose_taken_command(session, event.id)
            session.commit()
            result = dose_view_by_public_id(session, dose_event_id)
        return ui_success(UiDoseResponse, {"dose": result})

    router.include_router(create_patient_ui_router(get_runtime))

    return router


def _require_simulation_ready(session: Session) -> None:
    if not can_run_simulation(session):
        raise ui_http_error(
            409,
            "SIMULATION_NOT_READY",
            "A test medication scenario is required before running the simulation.",
        )
