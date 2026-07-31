from __future__ import annotations

import asyncio
import json
import threading
from datetime import date, datetime

from sqlalchemy import select

from shared.async_v13_contracts import (
    AsyncEventAccepted,
    DailyMedicationPatternAnalysisRequest,
)
from system_app.models import AgentJob, MedicationPlan
from system_app.services import workers
from system_app.services import agent_client as agent_client_module
from system_app.services.agent_client import AgentClient
from system_app.services.clock_service import ensure_clock
from system_app.services.daily_pattern_scheduler import (
    DAILY_PATTERN_JOB_TYPE,
    active_daily_pattern_patient_ids,
    daily_pattern_request_id,
    ensure_due_daily_pattern_job,
)
from tests.helpers import build_session, build_threadsafe_session_factory


def _active_plan(
    *,
    patient_id: str,
    start_date: date,
    end_date: date,
    active: bool = True,
) -> MedicationPlan:
    return MedicationPlan(
        patient_id=patient_id,
        medication_name="테스트 약",
        start_date=start_date,
        end_date=end_date,
        active=active,
    )


def test_backend_daily_pattern_scheduler_stages_previous_day_at_0200_once():
    analysis_date = date(2026, 7, 28)
    first_patient = "patient_0000000000000001"
    second_patient = "patient_0000000000000002"
    with build_session() as session:
        session.add_all(
            [
                _active_plan(
                    patient_id=second_patient,
                    start_date=date(2026, 7, 1),
                    end_date=date(9999, 12, 31),
                ),
                _active_plan(
                    patient_id=first_patient,
                    start_date=analysis_date,
                    end_date=analysis_date,
                ),
                _active_plan(
                    patient_id="patient_0000000000000003",
                    start_date=date(2026, 7, 1),
                    end_date=date(9999, 12, 31),
                    active=False,
                ),
            ]
        )
        session.flush()

        before = ensure_due_daily_pattern_job(
            session,
            current_time=datetime(2026, 7, 29, 1, 59),
        )
        first = ensure_due_daily_pattern_job(
            session,
            current_time=datetime(2026, 7, 29, 2, 0),
        )
        duplicate = ensure_due_daily_pattern_job(
            session,
            current_time=datetime(2026, 7, 29, 12, 0),
        )

        assert before is None
        assert first is not None
        assert duplicate is first
        assert first.request_id == daily_pattern_request_id(analysis_date)
        assert first.job_type == DAILY_PATTERN_JOB_TYPE
        assert first.status == "pending"
        assert json.loads(first.payload_json) == {
            "request_id": first.request_id,
            "patient_id": [first_patient, second_patient],
            "analysis_date": analysis_date.isoformat(),
        }
        assert session.query(AgentJob).count() == 1


def test_backend_daily_pattern_scheduler_marks_no_active_patient_day_done():
    with build_session() as session:
        job = ensure_due_daily_pattern_job(
            session,
            current_time=datetime(2026, 7, 29, 2, 0),
        )

        assert job is not None
        assert job.status == "done"
        assert job.error_message == "no_active_patients"
        assert job.completed_at is not None
        assert json.loads(job.payload_json)["patient_id"] == []


def test_active_daily_pattern_patients_use_analysis_date_validity():
    with build_session() as session:
        session.add_all(
            [
                _active_plan(
                    patient_id="patient_0000000000000001",
                    start_date=date(2026, 7, 28),
                    end_date=date(2026, 7, 28),
                ),
                _active_plan(
                    patient_id="patient_0000000000000002",
                    start_date=date(2026, 7, 29),
                    end_date=date(9999, 12, 31),
                ),
            ]
        )
        session.flush()

        assert active_daily_pattern_patient_ids(
            session,
            analysis_date=date(2026, 7, 28),
        ) == ["patient_0000000000000001"]


def test_agent_client_posts_daily_pattern_to_v13_acceptance_path(
    monkeypatch,
):
    captured: dict[str, object] = {}

    class AcceptedResponse:
        status_code = 202

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "request_id": "req_0000000000000001",
                "status": "accepted",
            }

    class CapturingClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            assert timeout == 10.0
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            captured.update(
                {
                    "url": url,
                    "json": json,
                    "headers": headers,
                }
            )
            return AcceptedResponse()

    monkeypatch.setattr(
        agent_client_module.httpx,
        "AsyncClient",
        CapturingClient,
    )
    request = DailyMedicationPatternAnalysisRequest(
        request_id="req_0000000000000001",
        patient_id=["patient_0000000000000001"],
        analysis_date=date(2026, 7, 28),
    )

    accepted = asyncio.run(
        AgentClient(
            "http://agent.test"
        ).send_daily_pattern_analysis_async(request)
    )

    assert accepted == AsyncEventAccepted(
        request_id=request.request_id,
        status="accepted",
    )
    assert captured == {
        "url": (
            "http://agent.test"
            "/agent/async/daily-medication-pattern-analysis"
        ),
        "json": request.model_dump(mode="json"),
        "headers": {
            "Authorization": "Bearer pytest-agent-sync-token",
        },
    }


def test_backend_worker_dispatches_due_daily_pattern_and_marks_job_done(
    monkeypatch,
):
    session_factory = build_threadsafe_session_factory()
    with session_factory() as session:
        session.add(
            _active_plan(
                patient_id="patient_0000000000000001",
                start_date=date(2026, 7, 1),
                end_date=date(9999, 12, 31),
            )
        )
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 7, 29, 2, 0)
        session.commit()

    stop_event = threading.Event()

    class AcceptingClient:
        def __init__(self) -> None:
            self.requests: list[
                DailyMedicationPatternAnalysisRequest
            ] = []

        async def send_daily_pattern_analysis_async(self, payload):
            self.requests.append(payload)
            stop_event.set()
            return AsyncEventAccepted(
                request_id=payload.request_id,
                status="accepted",
            )

        async def send_medication_event_async(self, _payload):
            raise AssertionError("unexpected missed-dose dispatch")

    client = AcceptingClient()
    monkeypatch.setattr(workers, "SessionLocal", session_factory)

    thread = threading.Thread(
        target=workers.agent_worker,
        args=(stop_event, threading.RLock(), client),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.analysis_date == date(2026, 7, 28)
    assert request.patient_id == ["patient_0000000000000001"]
    with session_factory() as session:
        job = session.scalar(
            select(AgentJob).where(
                AgentJob.request_id == request.request_id
            )
        )
        assert job is not None
        assert job.status == "done"
        assert job.attempts == 1
