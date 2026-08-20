from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from shared.chat_contracts import ChatMessageContent, ChatSyncResponse
from shared.settings import get_settings
from system_app import main as system_main
from system_app.db import get_session
from system_app.models import (
    BackendApiRequest,
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    Notification,
    NotificationPolicyChangeProposal,
    NutritionProfile,
)
from system_app.routes.ui_api import create_ui_api_router
from system_app.services import ui_status_service
from system_app.services.agent_client import AgentServiceError
from system_app.services.clock_service import ensure_clock
from system_app.services.dose_event_service import (
    collect_missed_dose_payloads,
    ensure_day_events,
    generate_notifications_for_new_events,
)
from system_app.services.missed_dose_flag_service import (
    activate_missed_dose_flag,
)
from system_app.services.nutrition_service import (
    nutrition_dashboard_view,
    run_nutrition_scenario,
)
from system_app.services.timeline_service import (
    ensure_chat_message_for_conversation_alert,
)
from system_app.services.ui_medication_scenario_service import (
    apply_test_medication_scenario,
    ensure_initial_testbed_scenario_state,
    list_test_medication_scenarios,
    seed_test_medication_scenarios,
)
from system_app.services.ui_policy_service import (
    MEDICATION_SCHEDULE_ALERT,
    MISSED_DOSE_CONVERSATION,
    set_ui_policy,
)
from system_app.services.ui_view_service import display_message_at
from system_app.ui_contracts import (
    UiChatHistoryResponse,
    UiChatSyncResponse,
    UiClockResponse,
    UiDashboardResponse,
    UiDoseResponse,
    UiMedicationScenarioApplyResponse,
    UiMedicationScenarioListResponse,
    UiNotificationAcknowledgementResponse,
    UiNotificationDetailResponse,
    UiNotificationListResponse,
    UiNutritionResponse,
    UiPoliciesResponse,
    UiSystemStatusResponse,
    UiTestbedResetResponse,
)
from tests.support.ui import build_ui_app


class StubChatAgent:
    def __init__(self) -> None:
        self.requests = []

    async def send_sync_chat(self, request):
        self.requests.append(request)
        return ChatSyncResponse(
            request_id=request.request_id,
            message_id=request.message_id,
            message_type="text",
            message=ChatMessageContent(
                message_title=None,
                text="복약 질문에 답변했습니다.",
                tables=None,
                selections=None,
                inputs=None,
            ),
            message_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        )


class FailingChatAgent:
    async def send_sync_chat(self, _request):
        raise AgentServiceError(
            "provider unavailable",
            error_type="agent_network_error",
            retryable=True,
        )


def _ui_app(tmp_path, *, agent_client=None):
    return build_ui_app(tmp_path, agent_client=agent_client)


def _seed_baseline(sessions) -> None:
    with sessions() as session:
        seed_test_medication_scenarios(session)
        ensure_initial_testbed_scenario_state(session)
        session.commit()


def test_ui_status_reports_backend_and_ai_server_separately(
    tmp_path,
    monkeypatch,
) -> None:
    async def ready_probe(_base_url: str, _agent_sync_token: str):
        return ui_status_service._ServiceProbe(
            status="ready",
            evidence=(
                "AGENT_READINESS_HTTP_OK",
                "AGENT_READINESS_CONTRACT_1_4",
                "GENERATION_PROVIDER_OK",
            ),
        )

    monkeypatch.setattr(
        ui_status_service,
        "_probe_agent_readiness",
        ready_probe,
    )
    client, _sessions = _ui_app(tmp_path)

    response = client.get("/api/ui/v1/status")

    assert response.status_code == 200
    payload = UiSystemStatusResponse.model_validate(response.json())
    assert payload.data.backend_server.status == "ready"
    assert payload.data.ai_server.status == "ready"
    assert payload.data.backend_server.checked_at
    assert payload.data.ai_server.checked_at
    assert payload.data.backend_server.evidence == [
        "REQUEST_ROUND_TRIP_OK",
        "DATABASE_PROBE_OK",
    ]
    assert "GENERATION_PROVIDER_OK" in payload.data.ai_server.evidence


def test_ui_status_keeps_backend_available_when_ai_is_not_ready(
    tmp_path,
    monkeypatch,
) -> None:
    async def not_ready_probe(
        _base_url: str,
        _agent_sync_token: str,
    ):
        return ui_status_service._ServiceProbe(
            status="not_ready",
            evidence=(
                "AGENT_READINESS_HTTP_503",
                "GENERATION_PROVIDER_INVOCATION_FAILED",
            ),
        )

    monkeypatch.setattr(
        ui_status_service,
        "_probe_agent_readiness",
        not_ready_probe,
    )
    client, _sessions = _ui_app(tmp_path)

    response = client.get("/api/ui/v1/status")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["backend_server"]["status"] == "ready"
    assert data["ai_server"]["status"] == "not_ready"
    assert "GENERATION_PROVIDER_INVOCATION_FAILED" in data["ai_server"][
        "evidence"
    ]


def test_testbed_reset_is_confirmed_idempotent_and_preserves_catalog(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    _seed_baseline(sessions)
    with sessions() as session:
        session.query(NutritionProfile).delete()
        session.commit()
    request = {
        "request_id": "req_0000000000000101",
        "confirm": True,
    }

    first = client.post("/api/ui/v1/testbed/reset", json=request)
    replay = client.post("/api/ui/v1/testbed/reset", json=request)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    result = UiTestbedResetResponse.model_validate(first.json())
    assert result.data.request_id == "req_0000000000000101"
    assert result.data.reset_applied is True

    with sessions() as session:
        assert session.query(MedicationPlan).count() == 0
        assert session.query(DoseEvent).count() == 0
        assert session.query(ChatMessage).count() == 0
        assert session.query(Notification).count() == 0
        assert (
            session.query(NutritionProfile)
            .filter_by(patient_id=get_settings().patient_id)
            .count()
            == 1
        )
        assert len(list_test_medication_scenarios(session)) == 5
        clock = ensure_clock(session)
        assert clock.is_running is False
        assert clock.speed_multiplier == 0
        request_rows = list(
            session.scalars(
                select(BackendApiRequest).where(
                    BackendApiRequest.api_path
                    == "/api/ui/v1/testbed/reset",
                )
            ).all()
        )
        assert len(request_rows) == 1
        assert request_rows[0].status == "COMPLETED"

    dashboard = client.get("/api/ui/v1/dashboard").json()["data"]
    history = client.get("/api/ui/v1/chat/history").json()["data"]
    assert dashboard["active_scenario"] is None
    assert dashboard["medications"] == []
    assert dashboard["simulation_ready"] is False
    assert history["days"][0]["messages"] == []


def test_testbed_reset_clears_notification_policy_change_proposals(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    _seed_baseline(sessions)
    with sessions() as session:
        notification = Notification(
            patient_id=get_settings().patient_id,
            notification_type="policy_change_proposal",
            title="알림 정책 변경 제안",
            body="테스트 제안",
            visible_at=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        )
        session.add(notification)
        session.flush()
        session.add(
            NotificationPolicyChangeProposal(
                request_id="req_0000000000000199",
                callback_hash="a" * 64,
                patient_id=get_settings().patient_id,
                proposed_policy_json="{}",
                reason="리셋 외래키 회귀 테스트",
                notification_id=notification.id,
            )
        )
        session.commit()

    response = client.post(
        "/api/ui/v1/testbed/reset",
        json={
            "request_id": "req_0000000000000109",
            "confirm": True,
        },
    )

    assert response.status_code == 200
    with sessions() as session:
        assert session.query(NotificationPolicyChangeProposal).count() == 0
        assert session.query(Notification).count() == 0


def test_testbed_reset_rejects_in_flight_chat_without_deleting_state(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    _seed_baseline(sessions)
    with sessions() as session:
        session.add(
            ChatMessage(
                public_id="user_msg_0000000000000010",
                patient_id=get_settings().patient_id,
                ai_request_id="pending-reset-guard",
                role="user",
                sender_type="patient",
                category="multiturn_chat",
                message_type="text",
                content="처리 중인 질문",
                processing_status="pending",
            )
        )
        session.commit()

    response = client.post(
        "/api/ui/v1/testbed/reset",
        json={
            "request_id": "req_0000000000000102",
            "confirm": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TESTBED_BUSY"
    assert response.json()["error"]["retryable"] is True
    with sessions() as session:
        assert session.query(MedicationPlan).count() == 4
        assert session.query(ChatMessage).count() == 1


def test_scenarios_are_db_backed_and_initial_fixture_matches_html(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    _seed_baseline(sessions)

    response = client.get("/api/ui/v1/medication-scenarios")
    assert response.status_code == 200
    scenarios = response.json()["data"]["scenarios"]
    assert [row["scenario_id"] for row in scenarios] == [
        "tb-diabetes-v1",
        "tb-hypertension-v1",
        "tb-kidney-cancer-v1",
        "tb-breast-cancer-v1",
        "tb-combined-v1",
    ]
    assert [
        item["medication_id"]
        for item in scenarios[-1]["medications"]
    ] == [
        "med-metformin-500",
        "med-amlodipine-5",
        "med-sunitinib-50",
        "med-letrozole-2-5",
    ]
    assert [
        item["treatment_area"]
        for item in scenarios[-1]["medications"]
    ] == [
        "당뇨약",
        "고혈압약",
        "신장암 치료약",
        "유방암 치료약",
    ]
    assert [
        item["scheduled_time"]
        for item in scenarios[-1]["medications"]
    ] == ["08:00", "09:00", "13:00", "18:00"]

    with sessions() as session:
        plans = list(
            session.scalars(
                select(MedicationPlan)
                .where(
                    MedicationPlan.source_type == "testbed_scenario"
                )
                .order_by(MedicationPlan.id)
            ).all()
        )
        events = list(
            session.scalars(
                select(DoseEvent).order_by(
                    DoseEvent.scheduled_for,
                    DoseEvent.id,
                )
            ).all()
        )
        assert len(plans) == 4
        assert {plan.treatment_area for plan in plans} == {
            "당뇨약",
            "고혈압약",
            "신장암 치료약",
            "유방암 치료약",
        }
        assert events[0].status == "taken"
        assert all(event.status == "scheduled" for event in events[1:])
        clock = ensure_clock(session)
        assert clock.is_running is True
        assert clock.speed_multiplier == 60


def test_ui_nutrition_projection_recursively_hides_internal_identity(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    _seed_baseline(sessions)
    with sessions() as session:
        run_nutrition_scenario(session, "normal_breakfast")
        internal = nutrition_dashboard_view(
            session,
            patient_id=get_settings().patient_id,
        )
        session.commit()

    assert internal["profile"]["patient_id"] == get_settings().patient_id
    assert isinstance(internal["meals"][0]["id"], int)
    assert isinstance(internal["meals"][0]["foods"][0]["version"], int)

    dashboard = client.get("/api/ui/v1/dashboard")
    nutrition = client.get("/api/ui/v1/nutrition")
    assert dashboard.status_code == 200
    assert nutrition.status_code == 200
    projected_views = [
        dashboard.json()["data"]["nutrition"],
        nutrition.json()["data"],
    ]

    def assert_no_internal_identity(value) -> None:
        if isinstance(value, list):
            for item in value:
                assert_no_internal_identity(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            assert key not in {
                "id",
                "patient_id",
                "version",
                "source_trace_id",
            }
            assert not (key.endswith("_id") and isinstance(item, int))
            assert_no_internal_identity(item)

    for projected in projected_views:
        assert_no_internal_identity(projected)
        assert projected["meals"][0]["foods"][0]["food_ref_id"]


def test_scenario_apply_preserves_manual_plan_and_does_not_auto_take(
    tmp_path,
) -> None:
    _client, sessions = _ui_app(tmp_path)
    _seed_baseline(sessions)

    with sessions() as session:
        clock = ensure_clock(session)
        manual = MedicationPlan(
            patient_id=get_settings().patient_id,
            medication_name="수동 등록약",
            dosage="1정",
            instructions="",
            treatment_area="기타",
            source_type="manual",
            source_key="",
            start_date=clock.current_time.date(),
            end_date=clock.current_time.date(),
            active=True,
        )
        session.add(manual)
        session.flush()
        session.add(
            DoseSchedule(
                plan_id=manual.id,
                slot_label="점심",
                scheduled_time="12:00",
            )
        )
        session.flush()
        ensure_day_events(session, clock.current_time.date())
        session.commit()

        result = apply_test_medication_scenario(
            session,
            scenario_id="tb-diabetes-v1",
            schedule_date=clock.current_time.date(),
        )
        session.commit()

        plans = list(
            session.scalars(
                select(MedicationPlan).order_by(MedicationPlan.id)
            ).all()
        )
        assert [plan.source_type for plan in plans] == [
            "manual",
            "testbed_scenario",
        ]
        assert plans[1].treatment_area == "당뇨약"
        scenario_events = [
            event
            for event in result["medications"]
            if event["medication_name"] == "메트포르민 500mg"
        ]
        assert scenario_events[0]["status"] == "scheduled"
        assert scenario_events[0]["taken_at"] is None


def test_scenario_apply_request_id_is_idempotent_and_conflict_safe(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    with sessions() as session:
        seed_test_medication_scenarios(session)
        schedule_date = ensure_clock(session).current_time.date()
        session.commit()

    body = {
        "request_id": "req_0000000000000103",
        "scenario_id": "tb-combined-v1",
        "schedule_date": schedule_date.isoformat(),
    }
    first = client.post(
        "/api/ui/v1/medication-scenarios/apply",
        json=body,
    )
    replay = client.post(
        "/api/ui/v1/medication-scenarios/apply",
        json=body,
    )
    conflict = client.post(
        "/api/ui/v1/medication-scenarios/apply",
        json={**body, "scenario_id": "tb-diabetes-v1"},
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with sessions() as session:
        assert session.query(MedicationPlan).count() == 4
        assert session.query(BackendApiRequest).count() == 1
        profile = session.scalar(
            select(NutritionProfile).where(
                NutritionProfile.patient_id == get_settings().patient_id
            )
        )
        assert profile is not None


def test_ui_policy_toggles_gate_notification_runtime_paths(
    tmp_path,
) -> None:
    _client, sessions = _ui_app(tmp_path)
    with sessions() as session:
        seed_test_medication_scenarios(session)
        clock = ensure_clock(session)
        apply_test_medication_scenario(
            session,
            scenario_id="tb-combined-v1",
            schedule_date=clock.current_time.date(),
        )
        set_ui_policy(
            session,
            MEDICATION_SCHEDULE_ALERT,
            False,
        )
        generate_notifications_for_new_events(
            session,
            clock.current_time + timedelta(days=1),
        )
        session.commit()

        assert (
            session.query(Notification)
            .filter_by(notification_type="medication_alert")
            .count()
            == 0
        )
        assert all(
            event.alerts_generated
            for event in session.scalars(select(DoseEvent)).all()
        )

    missed_path = tmp_path / "missed"
    missed_path.mkdir()
    _client, sessions = _ui_app(missed_path)
    with sessions() as session:
        seed_test_medication_scenarios(session)
        clock = ensure_clock(session)
        apply_test_medication_scenario(
            session,
            scenario_id="tb-diabetes-v1",
            schedule_date=clock.current_time.date(),
        )
        set_ui_policy(
            session,
            MISSED_DOSE_CONVERSATION,
            False,
        )
        payloads = collect_missed_dose_payloads(
            session,
            clock.current_time + timedelta(hours=3),
        )
        session.commit()

        event = session.scalar(select(DoseEvent))
        assert payloads == []
        assert event is not None
        assert event.status == "missed"
        assert (
            session.query(Notification)
            .filter_by(notification_type="conversation_alert")
            .count()
            == 0
        )


def test_notification_ack_all_stays_empty_and_history_shape_is_stable(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    with sessions() as session:
        clock = ensure_clock(session)
        now = clock.current_time
        source_display_at = _db_display_at(
            now.replace(tzinfo=get_settings_timezone())
        )
        reply_display_at = _db_display_at(
            now.replace(tzinfo=get_settings_timezone())
            + timedelta(minutes=1)
        )
        session.add(
            Notification(
                patient_id=get_settings().patient_id,
                notification_type="medication_alert",
                title="복약 예정",
                body="복약 시간이 다가옵니다.",
                visible_at=now,
                acknowledged=False,
                metadata_json=json.dumps({"severity": "reminder"}),
            )
        )
        session.add_all(
            [
                ChatMessage(
                    public_id="assistant_msg_0000000000000010",
                    patient_id=get_settings().patient_id,
                    ai_request_id="ui-history-request",
                    role="assistant",
                    sender_type="assistant",
                    message_type="selection_box",
                    content="변경할까요?",
                    message_payload_json=json.dumps(
                        {
                            "message_title": "복약 기록 변경",
                            "text": "변경할까요?",
                            "tables": None,
                            "selections": ["변경 적용", "취소"],
                            "inputs": None,
                        },
                        ensure_ascii=False,
                    ),
                    processing_status="completed",
                    display_at=source_display_at,
                    metadata_json=json.dumps(
                        {
                            "pending_response_status": "answered",
                            "response_message_id": (
                                "user_msg_0000000000000011"
                            ),
                            "feedback_reaction": "like",
                            "opinion_submitted": True,
                            "opinion_submitted_at": "2026-04-20T08:01:00+09:00",
                        }
                    ),
                    created_at=datetime(2026, 7, 26, 12, 0),
                ),
                ChatMessage(
                    public_id="user_msg_0000000000000011",
                    patient_id=get_settings().patient_id,
                    ai_request_id="ui-history-reply-request",
                    role="user",
                    sender_type="patient",
                    message_type="selection_box",
                    content="변경 적용",
                    processing_status="completed",
                    display_at=reply_display_at,
                    metadata_json=json.dumps(
                        {
                            "source_chat_message_id": (
                                "assistant_msg_0000000000000010"
                            ),
                        }
                    ),
                    created_at=datetime(2026, 7, 26, 12, 1),
                ),
                ChatMessage(
                    public_id="assistant_msg_0000000000000012",
                    patient_id=get_settings().patient_id,
                    role="assistant",
                    sender_type="assistant",
                    content="노출되면 안 됨",
                    display_at=reply_display_at + timedelta(minutes=1),
                    created_at=now,
                ),
            ]
        )
        session.flush()
        history_source = session.scalar(
            select(ChatMessage).where(
                ChatMessage.public_id == "assistant_msg_0000000000000010"
            )
        )
        history_reply = session.scalar(
            select(ChatMessage).where(
                ChatMessage.public_id == "user_msg_0000000000000011"
            )
        )
        assert history_source is not None
        assert history_reply is not None
        history_reply.reply_to_message_id = history_source.id
        session.commit()

    notifications = client.get("/api/ui/v1/notifications").json()["data"]
    assert len(notifications["notifications"]) == 1
    notification = notifications["notifications"][0]
    assert set(notification) == {
        "id",
        "notification_type",
        "title",
        "body",
        "visible_at",
        "visible_at_label",
        "acknowledged",
        "metadata",
        "interaction",
        "dose_status",
        "related_dose_event_id",
    }
    assert notification["id"].startswith("notif_")
    assert notifications["last_seen_id"].startswith("notif_")
    assert notification["visible_at"].endswith("+09:00")

    acknowledged = client.post(
        "/api/ui/v1/notifications/ack-all",
        json={},
    )
    assert acknowledged.json()["data"]["acknowledged_count"] == 1
    assert (
        client.get("/api/ui/v1/notifications")
        .json()["data"]["notifications"]
        == []
    )

    history = client.get("/api/ui/v1/chat/history").json()["data"]
    assert len(history["days"]) == 1
    assert len(history["days"][0]["messages"]) == 3
    message = next(
        item
        for item in history["days"][0]["messages"]
        if item["message_id"] == "assistant_msg_0000000000000010"
    )
    reply = next(
        item
        for item in history["days"][0]["messages"]
        if item["message_id"] == "user_msg_0000000000000011"
    )
    assert message["message"] == "변경할까요?"
    assert message["content"]["selections"] == ["변경 적용", "취소"]
    assert message["processing_status"] == "answered"
    assert message["response_message_id"] == "user_msg_0000000000000011"
    assert message["source_message_id"] is None
    assert reply["response_message_id"] is None
    assert reply["source_message_id"] == "assistant_msg_0000000000000010"
    assert message["sort_sequence"] < reply["sort_sequence"]
    assert message["reaction"] == "like"
    assert message["opinion_submitted"] is True
    assert message["created_at"].endswith("+09:00")


def test_ui_structured_chat_requires_explicit_source_message_id(
    tmp_path,
) -> None:
    agent = StubChatAgent()
    client, _sessions = _ui_app(tmp_path, agent_client=agent)

    response = client.post(
        "/api/ui/v1/chat/sync",
        json={
            "message": "기록",
            "requested_return_type": "selection_box",
            "request_id": "req_0000000000000104",
        },
    )

    assert response.status_code == 422
    assert "source_message_id_required_for_structured_response" in (
        response.text
    )
    assert agent.requests == []


def test_ui_notifications_are_newest_first_and_keep_after_id_cursor(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    with sessions() as session:
        now = ensure_clock(session).current_time
        oldest = Notification(
            patient_id=get_settings().patient_id,
            notification_type="medication_alert",
            title="오래된 알림",
            body="먼저 생성된 알림",
            visible_at=now - timedelta(minutes=20),
            acknowledged=False,
            metadata_json=json.dumps({"severity": "reminder"}),
        )
        newest = Notification(
            patient_id=get_settings().patient_id,
            notification_type="agent_error",
            title="최신 중요 알림",
            body="가장 최근에 표시된 알림",
            visible_at=now - timedelta(minutes=1),
            acknowledged=False,
            metadata_json=json.dumps({"severity": "critical"}),
        )
        middle = Notification(
            patient_id=get_settings().patient_id,
            notification_type="medication_alert",
            title="중간 알림",
            body="두 번째로 최근 알림",
            visible_at=now - timedelta(minutes=5),
            acknowledged=False,
            metadata_json=json.dumps({"severity": "reminder"}),
        )
        hidden = Notification(
            patient_id=get_settings().patient_id,
            notification_type="medication_alert",
            title="내부 알림",
            body="환자 화면에 노출되지 않음",
            visible_at=now,
            acknowledged=False,
            metadata_json=json.dumps(
                {
                    "severity": "critical",
                    "delivery_channel": "internal_only",
                }
            ),
        )
        session.add_all([oldest, newest, middle, hidden])
        session.flush()
        expected_ids = [
            newest.public_id,
            middle.public_id,
            oldest.public_id,
        ]
        expected_cursor = hidden.public_id
        session.commit()

    response = client.get("/api/ui/v1/notifications?limit=3")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["id"] for row in data["notifications"]] == expected_ids
    assert data["notifications"][0]["metadata"]["severity"] == "critical"
    assert data["last_seen_id"] == expected_cursor

    repeated = client.get(
        f"/api/ui/v1/notifications?after_id={expected_cursor}&limit=3"
    )
    assert repeated.json()["data"]["notifications"] == []
    assert repeated.json()["data"]["last_seen_id"] == expected_cursor

    legacy_numeric_cursor = client.get(
        "/api/ui/v1/notifications?after_id=1&limit=3"
    )
    assert legacy_numeric_cursor.status_code == 422
    unknown_cursor = client.get(
        "/api/ui/v1/notifications"
        "?after_id=notif_00000000000000000000000000000000&limit=3"
    )
    assert unknown_cursor.status_code == 422
    assert (
        unknown_cursor.json()["detail"]["code"]
        == "NOTIFICATION_CURSOR_INVALID"
    )

    detail = client.get(
        f"/api/ui/v1/notifications/{expected_ids[0]}"
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["notification"]["id"] == expected_ids[0]
    assert client.get("/api/ui/v1/notifications/1").status_code == 422
    assert (
        client.get(
            "/api/ui/v1/notifications/"
            "notif_00000000000000000000000000000000"
        ).status_code
        == 404
    )

    dashboard = client.get("/api/ui/v1/dashboard").json()["data"]
    assert [
        row["id"] for row in dashboard["notifications"][:3]
    ] == expected_ids


def test_missed_dose_notification_opens_continuous_chat_and_reply_resolves_it(
    tmp_path,
) -> None:
    agent = StubChatAgent()
    client, sessions = _ui_app(tmp_path, agent_client=agent)
    _seed_baseline(sessions)

    with sessions() as session:
        clock = ensure_clock(session)
        event = session.scalar(
            select(DoseEvent)
            .where(DoseEvent.patient_id == get_settings().patient_id)
            .order_by(DoseEvent.id)
        )
        assert event is not None
        event.status = "missed"
        event.missed_detected_at = clock.current_time
        activate_missed_dose_flag(
            session,
            event,
            activated_at=clock.current_time,
        )
        notification = Notification(
            patient_id=get_settings().patient_id,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="복약을 놓친 이유를 알려주세요.",
            visible_at=clock.current_time,
            acknowledged=False,
            related_dose_event_id=event.id,
            metadata_json=json.dumps(
                {
                    "category": "missed_dose",
                    "status": "agent_ready",
                },
                ensure_ascii=False,
            ),
        )
        session.add(notification)
        session.flush()
        prompt = ensure_chat_message_for_conversation_alert(
            session,
            notification,
        )
        assert prompt is not None
        notification_metadata = json.loads(notification.metadata_json)
        notification_metadata["chat_message_id"] = prompt.public_id
        notification.metadata_json = json.dumps(
            notification_metadata,
            ensure_ascii=False,
        )
        notification_internal_id = notification.id
        notification_public_id = notification.public_id
        prompt_id = prompt.public_id
        session.commit()

    projected = client.get("/api/ui/v1/notifications").json()["data"][
        "notifications"
    ]
    missed_notification = next(
        row
        for row in projected
        if row["id"] == notification_public_id
    )
    assert missed_notification["interaction"] == {
        "kind": "open_chat",
        "state": "ready",
        "message_id": prompt_id,
    }

    history = client.get(
        "/api/ui/v1/chat/history",
        params={"limit_days": 7},
    ).json()["data"]
    prompt_view = next(
        message
        for day in history["days"]
        for message in day["messages"]
        if message["message_id"] == prompt_id
    )
    assert "conversation_id" not in prompt_view
    assert prompt_view["message"] == "복약을 놓친 이유를 알려주세요."

    reply = client.post(
        "/api/ui/v1/chat/sync",
        json={
            "message": "깜빡했어요.",
            "requested_return_type": "text",
            "request_id": "req_0000000000000105",
            "source_message_id": prompt_id,
        },
    )
    assert reply.status_code == 200

    with sessions() as session:
        notification = session.get(
            Notification,
            notification_internal_id,
        )
        assert notification is not None
        assert notification.acknowledged is True
        metadata = json.loads(notification.metadata_json)
        assert metadata["status"] == "agent_response_completed"
        assert (
            metadata["assistant_message_id"]
            == reply.json()["data"]["assistant_message_id"]
        )
        assert "missed_dose_reply_understanding" not in metadata
        user_message = session.scalar(
            select(ChatMessage).where(
                ChatMessage.public_id
                == reply.json()["data"]["user_message_id"]
            )
        )
        assert user_message is not None
        user_metadata = json.loads(user_message.metadata_json)
        assert (
            user_metadata["missed_dose_reply"][
                "conversation_alert_id"
            ]
            == notification_internal_id
        )
        assert (
            user_metadata["missed_dose_reply"]["status"]
            == "pending_agent_interpretation"
        )
        assert "understanding" not in user_metadata["missed_dose_reply"]


def test_missed_dose_reply_keeps_alert_open_when_agent_fails(
    tmp_path,
) -> None:
    client, sessions = _ui_app(
        tmp_path,
        agent_client=FailingChatAgent(),
    )
    _seed_baseline(sessions)
    with sessions() as session:
        clock = ensure_clock(session)
        event = session.scalar(
            select(DoseEvent)
            .where(DoseEvent.patient_id == get_settings().patient_id)
            .order_by(DoseEvent.id)
        )
        assert event is not None
        event.status = "missed"
        event.missed_detected_at = clock.current_time
        activate_missed_dose_flag(
            session,
            event,
            activated_at=clock.current_time,
        )
        notification = Notification(
            patient_id=get_settings().patient_id,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="복약을 놓친 이유를 알려주세요.",
            visible_at=clock.current_time,
            acknowledged=False,
            related_dose_event_id=event.id,
            metadata_json=json.dumps(
                {"category": "missed_dose", "status": "agent_ready"}
            ),
        )
        session.add(notification)
        session.flush()
        prompt = ensure_chat_message_for_conversation_alert(
            session,
            notification,
        )
        assert prompt is not None
        notification_metadata = json.loads(
            notification.metadata_json
        )
        notification_metadata["chat_message_id"] = prompt.public_id
        notification.metadata_json = json.dumps(
            notification_metadata
        )
        notification_id = notification.id
        prompt_id = prompt.public_id
        session.commit()

    response = client.post(
        "/api/ui/v1/chat/sync",
        json={
            "message": "깜빡했어요.",
            "requested_return_type": "text",
            "request_id": "req_0000000000000106",
            "source_message_id": prompt_id,
        },
    )

    assert response.status_code == 502
    with sessions() as session:
        notification = session.get(Notification, notification_id)
        assert notification is not None
        assert notification.acknowledged is False
        metadata = json.loads(notification.metadata_json)
        assert metadata["status"] == "agent_ready"
        assert "missed_dose_reply_understanding" not in metadata
        user_message = session.scalar(
                select(ChatMessage).where(
                    ChatMessage.ai_request_id
                    == "req_0000000000000106"
                )
        )
        assert user_message is not None
        user_metadata = json.loads(user_message.metadata_json)
        reply_context = user_metadata["missed_dose_reply"]
        assert (
            reply_context["status"]
            == "pending_agent_interpretation"
        )
        assert "understanding" not in reply_context


def test_ack_all_keeps_unresolved_missed_dose_conversation_available(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    with sessions() as session:
        now = ensure_clock(session).current_time
        notification = Notification(
            patient_id=get_settings().patient_id,
            notification_type="conversation_alert",
            title="AI 대화 준비 중",
            body="미복용 대화를 준비하고 있습니다.",
            visible_at=now,
            acknowledged=False,
            metadata_json=json.dumps(
                {
                    "category": "missed_dose",
                    "status": "awaiting_agent",
                },
                ensure_ascii=False,
            ),
        )
        session.add(notification)
        session.flush()
        notification_id = notification.id
        session.commit()

    acknowledged = client.post(
        "/api/ui/v1/notifications/ack-all",
        json={},
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["data"]["acknowledged_count"] == 0
    with sessions() as session:
        notification = session.get(Notification, notification_id)
        assert notification is not None
        assert notification.acknowledged is False


def test_chat_history_initial_page_keeps_today_then_loads_prior_dates(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    with sessions() as session:
        today = ensure_clock(session).current_time.date()
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)
        two_days_ago_at = _db_display_at(
            datetime.combine(
                two_days_ago,
                datetime.min.time(),
            ).replace(
                hour=11,
                tzinfo=get_settings_timezone(),
            )
        )
        yesterday_at = _db_display_at(
            datetime.combine(
                yesterday,
                datetime.min.time(),
            ).replace(
                hour=11,
                tzinfo=get_settings_timezone(),
            )
        )
        session.add_all(
            [
                ChatMessage(
                    public_id="assistant_msg_0000000000000020",
                    patient_id=get_settings().patient_id,
                    role="assistant",
                    sender_type="assistant",
                    content="이틀 전 대화",
                    display_at=two_days_ago_at,
                ),
                ChatMessage(
                    public_id="assistant_msg_0000000000000021",
                    patient_id=get_settings().patient_id,
                    role="assistant",
                    sender_type="assistant",
                    content="어제 대화",
                    display_at=yesterday_at,
                ),
            ]
        )
        session.commit()

    initial = client.get("/api/ui/v1/chat/history").json()["data"]
    assert initial["days"] == [
        {"date": today.isoformat(), "messages": []}
    ]
    assert initial["next_before_date"] == today.isoformat()

    yesterday_page = client.get(
        "/api/ui/v1/chat/history",
        params={"before_date": today.isoformat()},
    ).json()["data"]
    assert [day["date"] for day in yesterday_page["days"]] == [
        yesterday.isoformat()
    ]
    assert yesterday_page["days"][0]["messages"][0]["message"] == "어제 대화"
    assert yesterday_page["next_before_date"] == yesterday.isoformat()

    two_days_ago_page = client.get(
        "/api/ui/v1/chat/history",
        params={"before_date": yesterday.isoformat()},
    ).json()["data"]
    assert [day["date"] for day in two_days_ago_page["days"]] == [
        two_days_ago.isoformat()
    ]
    assert (
        two_days_ago_page["days"][0]["messages"][0]["message"]
        == "이틀 전 대화"
    )
    assert two_days_ago_page["next_before_date"] is None


def test_chat_history_before_date_is_not_truncated_by_newer_2000_rows(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    with sessions() as session:
        today = ensure_clock(session).current_time.date()
        yesterday = today - timedelta(days=1)
        yesterday_at = _db_display_at(
            datetime.combine(yesterday, datetime.min.time())
            .replace(hour=11, tzinfo=get_settings_timezone())
        )
        today_at = _db_display_at(
            datetime.combine(today, datetime.min.time())
            .replace(hour=11, tzinfo=get_settings_timezone())
        )
        session.add(
            ChatMessage(
                public_id="assistant_msg_0000000000000030",
                patient_id=get_settings().patient_id,
                role="assistant",
                sender_type="assistant",
                content="대량 이력 이전 메시지",
                display_at=yesterday_at,
            )
        )
        session.flush()
        session.add_all(
            [
                ChatMessage(
                    public_id=(
                        f"assistant_msg_{index + 0x1000:016x}"
                    ),
                    patient_id=get_settings().patient_id,
                    role="assistant",
                    sender_type="assistant",
                    content=f"오늘 메시지 {index}",
                    display_at=today_at,
                )
                for index in range(2001)
            ]
        )
        session.commit()

    page = client.get(
        "/api/ui/v1/chat/history",
        params={"before_date": today.isoformat()},
    ).json()["data"]
    assert [day["date"] for day in page["days"]] == [
        yesterday.isoformat()
    ]
    assert [
        message["message"] for message in page["days"][0]["messages"]
    ] == ["대량 이력 이전 메시지"]


def test_chat_history_applies_patient_scoped_user_turn_limit(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    patient_id = get_settings().patient_id
    with sessions() as session:
        today = ensure_clock(session).current_time.date()
        for turn_number, hour in enumerate((9, 10, 11), start=1):
            display_date = (
                today - timedelta(days=1)
                if turn_number == 1
                else today
            )
            display_at = datetime.combine(
                display_date,
                datetime.min.time(),
            ).replace(
                hour=hour,
                tzinfo=get_settings_timezone(),
            )
            user = ChatMessage(
                public_id=f"user_msg_{turn_number + 0x2000:016x}",
                patient_id=patient_id,
                role="user",
                sender_type="patient",
                content=f"사용자 질문 {turn_number}",
                display_at=_db_display_at(display_at),
            )
            session.add(user)
            session.flush()
            session.add(
                ChatMessage(
                    public_id=(
                        f"assistant_msg_{turn_number + 0x3000:016x}"
                    ),
                    patient_id=patient_id,
                    role="assistant",
                    sender_type="assistant",
                    content=f"AI 답변 {turn_number}",
                    reply_to_message_id=user.id,
                    display_at=_db_display_at(display_at),
                )
            )
        system_display_at = datetime.combine(
            today,
            datetime.min.time(),
        ).replace(
            hour=10,
            minute=30,
            tzinfo=get_settings_timezone(),
        )
        session.add(
            ChatMessage(
                public_id="assistant_msg_0000000000004000",
                patient_id=patient_id,
                role="assistant",
                sender_type="system",
                content="시스템 안내",
                display_at=_db_display_at(system_display_at),
            )
        )
        session.add(
            ChatMessage(
                public_id="user_msg_0000000000005000",
                patient_id="patient_0000000000000002",
                role="user",
                sender_type="patient",
                content="다른 환자 질문",
                display_at=_db_display_at(
                    datetime.combine(
                        today,
                        datetime.min.time(),
                    ).replace(
                        hour=12,
                        tzinfo=get_settings_timezone(),
                    )
                ),
            )
        )
        session.commit()

    response = client.get(
        "/api/ui/v1/chat/history",
        params={"limit_days": 2, "limit_turns": 2},
    )

    assert response.status_code == 200
    days = response.json()["data"]["days"]
    assert [day["date"] for day in days] == [today.isoformat()]
    messages = days[0]["messages"]
    assert [message["message"] for message in messages] == [
        "사용자 질문 2",
        "AI 답변 2",
        "시스템 안내",
        "사용자 질문 3",
        "AI 답변 3",
    ]
    assert all(
        message["message_id"] != "other_patient_newest_turn"
        for message in messages
    )


def test_chat_history_rejects_invalid_turn_limit(tmp_path) -> None:
    client, _sessions = _ui_app(tmp_path)

    too_small = client.get(
        "/api/ui/v1/chat/history",
        params={"limit_turns": 0},
    )
    too_large = client.get(
        "/api/ui/v1/chat/history",
        params={"limit_turns": 201},
    )

    assert too_small.status_code == 422
    assert too_large.status_code == 422
    assert too_small.json()["detail"][0]["loc"][-1] == "limit_turns"
    assert too_large.json()["detail"][0]["loc"][-1] == "limit_turns"


def test_chat_history_projects_db_reply_links_across_date_pages(
    tmp_path,
) -> None:
    client, sessions = _ui_app(tmp_path)
    with sessions() as session:
        today = ensure_clock(session).current_time.date()
        yesterday = today - timedelta(days=1)
        source_at = (
            datetime.combine(yesterday, datetime.min.time())
            .replace(hour=23, minute=59, tzinfo=get_settings_timezone())
        )
        reply_at = (
            datetime.combine(today, datetime.min.time())
            .replace(minute=1, tzinfo=get_settings_timezone())
        )
        source = ChatMessage(
            public_id="assistant_msg_0000000000006000",
            patient_id=get_settings().patient_id,
            ai_request_id="cross-page-source-request",
            role="assistant",
            sender_type="assistant",
            message_type="selection_box",
            content="기록할까요?",
            message_payload_json=json.dumps(
                {
                    "message_title": "기록 확인",
                    "text": "기록할까요?",
                    "tables": None,
                    "selections": ["기록", "취소"],
                    "inputs": None,
                },
                ensure_ascii=False,
            ),
            processing_status="completed",
            display_at=_db_display_at(source_at),
            metadata_json=json.dumps(
                {
                    "pending_response_status": "answered",
                    "response_message_id": "user_msg_0000000000006001",
                }
            ),
        )
        session.add(source)
        session.flush()
        session.add(
            ChatMessage(
                public_id="user_msg_0000000000006002",
                patient_id=get_settings().patient_id,
                ai_request_id="cross-page-reply-request",
                role="user",
                sender_type="patient",
                message_type="selection_box",
                content="기록",
                message_payload_json=json.dumps({"text": "기록"}),
                reply_to_message_id=source.id,
                processing_status="completed",
                display_at=_db_display_at(reply_at),
                # The FK is canonical even if compatibility metadata drifts.
                metadata_json=json.dumps(
                    {
                        "source_chat_message_id": "assistant_wrong_legacy_source",
                    }
                ),
            )
        )
        session.commit()

    today_page = client.get("/api/ui/v1/chat/history").json()["data"]
    assert [day["date"] for day in today_page["days"]] == [
        today.isoformat()
    ]
    reply = today_page["days"][0]["messages"][0]
    assert reply["message_id"] == "user_msg_0000000000006002"
    assert reply["source_message_id"] == "assistant_msg_0000000000006000"
    assert today_page["next_before_date"] == today.isoformat()

    yesterday_page = client.get(
        "/api/ui/v1/chat/history",
        params={"before_date": today.isoformat()},
    ).json()["data"]
    source_view = yesterday_page["days"][0]["messages"][0]
    assert source_view["message_id"] == "assistant_msg_0000000000006000"
    assert source_view["response_message_id"] == "user_msg_0000000000006002"
    assert source_view["processing_status"] == "answered"


def test_ui_runtime_event_generation_is_scoped_to_configured_patient(
    tmp_path,
) -> None:
    _client, sessions = _ui_app(tmp_path)
    configured_patient = get_settings().patient_id
    other_patient = "patient_0000000000000002"

    with sessions() as session:
        clock = ensure_clock(session)
        target_date = clock.current_time.date()
        configured_plan = MedicationPlan(
            patient_id=configured_patient,
            medication_name="테스트 환자 약",
            dosage="1정",
            instructions="",
            start_date=target_date,
            end_date=target_date,
            active=True,
        )
        other_plan = MedicationPlan(
            patient_id=other_patient,
            medication_name="다른 환자 약",
            dosage="1정",
            instructions="",
            start_date=target_date,
            end_date=target_date,
            active=True,
        )
        session.add_all([configured_plan, other_plan])
        session.flush()
        configured_schedule = DoseSchedule(
            plan_id=configured_plan.id,
            slot_label="아침",
            scheduled_time="08:00",
        )
        other_schedule = DoseSchedule(
            plan_id=other_plan.id,
            slot_label="아침",
            scheduled_time="08:00",
        )
        session.add_all([configured_schedule, other_schedule])
        session.flush()

        ensure_day_events(session, target_date)
        configured_event = session.scalar(
            select(DoseEvent).where(
                DoseEvent.patient_id == configured_patient
            )
        )
        assert configured_event is not None
        assert (
            session.query(DoseEvent)
            .filter(DoseEvent.patient_id == other_patient)
            .count()
            == 0
        )

        other_event = DoseEvent(
            patient_id=other_patient,
            plan_id=other_plan.id,
            schedule_id=other_schedule.id,
            medication_name=other_plan.medication_name,
            slot_label=other_schedule.slot_label,
            scheduled_for=datetime.combine(
                target_date,
                datetime.strptime("08:00", "%H:%M").time(),
            ),
            status="scheduled",
            alerts_generated=False,
            missed_handled=False,
        )
        session.add(other_event)
        session.flush()

        set_ui_policy(
            session,
            MEDICATION_SCHEDULE_ALERT,
            False,
            patient_id=configured_patient,
        )
        set_ui_policy(
            session,
            MISSED_DOSE_CONVERSATION,
            False,
            patient_id=configured_patient,
        )
        processing_time = clock.current_time + timedelta(days=1)
        generate_notifications_for_new_events(session, processing_time)
        payloads = collect_missed_dose_payloads(
            session,
            processing_time,
        )
        session.commit()

        session.refresh(configured_event)
        session.refresh(other_event)
        assert configured_event.alerts_generated is True
        assert configured_event.status == "missed"
        assert configured_event.missed_handled is True
        assert other_event.patient_id == other_patient
        assert other_event.alerts_generated is False
        assert other_event.status == "scheduled"
        assert other_event.missed_handled is False
        assert payloads == []
        assert session.query(Notification).count() == 0


def test_chat_wrapper_injects_server_identity_and_simulation_display_time(
    tmp_path,
) -> None:
    agent = StubChatAgent()
    client, sessions = _ui_app(tmp_path, agent_client=agent)
    with sessions() as session:
        clock = ensure_clock(session)
        expected_display_at = clock.current_time.replace(
            tzinfo=get_settings_timezone()
        ).isoformat()
        session.commit()

    response = client.post(
        "/api/ui/v1/chat/sync",
        json={
            "message": "  오늘 약은 언제 먹어?  ",
            "requested_return_type": "text",
            "request_id": "req_0000000000000107",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert "conversation_id" not in data
    assert data["display_message_at"] == expected_display_at
    assert data["message_at"] == "2026-07-26T12:00:00Z"
    assert data["user_sort_sequence"] < data["assistant_sort_sequence"]
    assert len(agent.requests) == 1
    request = agent.requests[0]
    assert request.patient_id == get_settings().patient_id
    assert request.message == "오늘 약은 언제 먹어?"
    assert request.request_id == "req_0000000000000107"
    assert request.message_at.isoformat() == expected_display_at

    with sessions() as session:
        assistant = session.scalar(
            select(ChatMessage).where(
                ChatMessage.public_id == data["assistant_message_id"]
            )
        )
        metadata = json.loads(assistant.metadata_json)
        expected_conversation_at = (
            datetime.fromisoformat(expected_display_at)
            .astimezone(UTC)
            .replace(tzinfo=None)
        )
        assert assistant.id == data["assistant_sort_sequence"]
        assert assistant.conversation_at == expected_conversation_at
        assert assistant.display_at == expected_conversation_at
        assert assistant.recorded_at is not None
        assert metadata["message_at"] == "2026-07-26T12:00:00+00:00"
        assert metadata["display_message_at"] == expected_display_at


def test_chat_failure_returns_error_without_creating_fallback_assistant(
    tmp_path,
) -> None:
    client, sessions = _ui_app(
        tmp_path,
        agent_client=FailingChatAgent(),
    )

    response = client.post(
        "/api/ui/v1/chat/sync",
        json={
            "message": "오늘 약은 언제 먹어?",
            "requested_return_type": "text",
            "request_id": "req_0000000000000108",
        },
    )

    assert response.status_code == 502
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "agent_network_error"
    assert "대체 응답은 생성하지 않았습니다" in payload["error"]["message"]
    with sessions() as session:
        rows = session.scalars(
            select(ChatMessage).where(
                ChatMessage.ai_request_id == "req_0000000000000108"
            )
        ).all()
        assert [(row.role, row.processing_status) for row in rows] == [
            ("user", "retryable_failed")
        ]


def test_chat_history_uses_required_display_at_as_utc() -> None:
    message = ChatMessage(
        public_id="assistant_msg_0000000000007000",
        patient_id=get_settings().patient_id,
        role="assistant",
        sender_type="assistant",
        content="UTC fallback",
        display_at=datetime(2026, 7, 26, 12, 0),
        created_at=datetime(2026, 7, 26, 12, 0),
    )

    displayed_at = display_message_at(message)

    assert displayed_at.isoformat() == "2026-07-26T21:00:00+09:00"


def test_ui_validation_and_http_errors_use_uniform_envelope(
    tmp_path,
) -> None:
    _client, sessions = _ui_app(tmp_path)
    app = system_main.create_app()

    def session_override():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    client = TestClient(app)

    invalid = client.post(
        "/api/ui/v1/chat/sync",
        json={
            "message": "   ",
            "patient_id": "browser-controlled-patient",
        },
    )
    missing = client.put(
        "/api/ui/v1/policies/not-a-policy",
        json={"enabled": True},
    )
    invalid_reset = client.post(
        "/api/ui/v1/testbed/reset",
        json={
            "request_id": "req_0000000000000109",
            "confirm": False,
            "patient_id": "browser-controlled-patient",
        },
    )

    assert invalid.status_code == 422
    assert invalid.json()["success"] is False
    assert invalid.json()["data"] is None
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "UI_POLICY_NOT_FOUND"
    assert invalid_reset.status_code == 422
    assert invalid_reset.json()["error"]["code"] == "INVALID_REQUEST"


def get_settings_timezone():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Seoul")


def _db_display_at(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def test_ui_openapi_uses_strict_success_and_runtime_error_envelopes() -> None:
    app = FastAPI()
    runtime = SimpleNamespace(write_lock=threading.RLock(), agent_client=None)
    app.include_router(create_ui_api_router(lambda: runtime))
    document = app.openapi()

    success_schemas = {
        ("/api/ui/v1/dashboard", "get"): "UiDashboardResponse",
        ("/api/ui/v1/status", "get"): "UiSystemStatusResponse",
        (
            "/api/ui/v1/medication-scenarios",
            "get",
        ): "UiMedicationScenarioListResponse",
        (
            "/api/ui/v1/medication-scenarios/apply",
            "post",
        ): "UiMedicationScenarioApplyResponse",
        (
            "/api/ui/v1/testbed/reset",
            "post",
        ): "UiTestbedResetResponse",
        ("/api/ui/v1/clock/advance", "post"): "UiClockResponse",
        ("/api/ui/v1/clock/play", "post"): "UiClockResponse",
        ("/api/ui/v1/clock/pause", "post"): "UiClockResponse",
        (
            "/api/ui/v1/doses/{dose_event_id}/take",
            "post",
        ): "UiDoseResponse",
        ("/api/ui/v1/nutrition", "get"): "UiNutritionResponse",
        ("/api/ui/v1/policies", "get"): "UiPoliciesResponse",
        (
            "/api/ui/v1/policies/{policy_key}",
            "put",
        ): "UiPoliciesResponse",
        (
            "/api/ui/v1/notifications",
            "get",
        ): "UiNotificationListResponse",
        (
            "/api/ui/v1/notifications/{notification_id}",
            "get",
        ): "UiNotificationDetailResponse",
        (
            "/api/ui/v1/notifications/ack-all",
            "post",
        ): "UiNotificationAcknowledgementResponse",
        (
            "/api/ui/v1/chat/history",
            "get",
        ): "UiChatHistoryResponse",
        ("/api/ui/v1/chat/sync", "post"): "UiChatSyncResponse",
    }
    for (path, method), schema_name in success_schemas.items():
        operation = document["paths"][path][method]
        success_schema = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert success_schema["$ref"].endswith(f"/{schema_name}")
        envelope_schema = document["components"]["schemas"][schema_name]
        assert set(envelope_schema["properties"]) == {
            "success",
            "data",
            "error",
        }
        assert envelope_schema["additionalProperties"] is False
        assert set(envelope_schema["required"]) == {
            "success",
            "data",
            "error",
        }
        for status_code, response in operation["responses"].items():
            if status_code == "200":
                continue
            error_schema = response["content"]["application/json"]["schema"]
            assert error_schema["$ref"].endswith("/UiErrorResponse")
        assert "HTTPValidationError" not in json.dumps(operation)

    components = document["components"]["schemas"]
    exact_properties = {
        "UiDashboardData": {
            "features",
            "clock",
            "simulation_ready",
            "active_scenario",
            "medications",
            "nutrition",
            "policies",
            "notifications",
        },
        "UiFeatureFlags": {
            "medication_side_effect_enabled",
        },
        "UiSimulationClock": {
            "current_time",
            "is_running",
            "speed_multiplier",
        },
        "UiMedicationDose": {
            "dose_event_id",
            "medication_name",
            "treatment_area",
            "slot_label",
            "scheduled_for",
            "status",
            "taken_at",
        },
        "UiPolicies": {
            "medication_schedule_alert",
            "missed_dose_conversation",
        },
        "UiSystemStatusData": {
            "backend_server",
            "ai_server",
        },
        "UiBackendServerStatus": {
            "status",
            "checked_at",
            "evidence",
        },
        "UiAiServerStatus": {
            "status",
            "checked_at",
            "evidence",
        },
        "UiTestbedResetData": {
            "request_id",
            "reset_applied",
            "reset_at",
        },
        "UiNotification": {
            "id",
            "notification_type",
            "title",
            "body",
            "visible_at",
            "visible_at_label",
            "acknowledged",
            "metadata",
            "interaction",
            "dose_status",
            "related_dose_event_id",
        },
        "UiNotificationInteraction": {
            "kind",
            "state",
            "message_id",
        },
        "UiChatSyncData": {
            "request_id",
            "user_message_id",
            "user_sort_sequence",
            "assistant_message_id",
            "assistant_sort_sequence",
            "message_type",
            "message",
            "message_at",
            "display_message_at",
        },
        "UiChatHistoryMessage": {
            "message_id",
            "sort_sequence",
            "role",
            "message_type",
            "message",
            "content",
            "created_at",
            "processing_status",
            "response_message_id",
            "source_message_id",
            "reaction",
            "opinion_submitted",
            "opinion_submitted_at",
        },
    }
    for schema_name, properties in exact_properties.items():
        schema = components[schema_name]
        assert set(schema["properties"]) == properties
        assert schema["additionalProperties"] is False

    notification_id_schema = components["UiNotification"]["properties"][
        "id"
    ]
    assert notification_id_schema["type"] == "string"
    assert (
        notification_id_schema["pattern"]
        == r"^notif_[0-9a-f]{32}$"
    )
    notification_list_operation = document["paths"][
        "/api/ui/v1/notifications"
    ]["get"]
    after_id_parameter = next(
        parameter
        for parameter in notification_list_operation["parameters"]
        if parameter["name"] == "after_id"
    )
    assert after_id_parameter["in"] == "query"
    assert after_id_parameter["schema"]["anyOf"][0] == {
        "type": "string",
        "pattern": r"^notif_[0-9a-f]{32}$",
    }
    detail_id_parameter = document["paths"][
        "/api/ui/v1/notifications/{notification_id}"
    ]["get"]["parameters"][0]
    assert detail_id_parameter["schema"]["type"] == "string"
    assert (
        detail_id_parameter["schema"]["pattern"]
        == r"^notif_[0-9a-f]{32}$"
    )

    error_response = components["UiErrorResponse"]
    error = components["UiError"]
    assert set(error_response["properties"]) == {"success", "data", "error"}
    assert error_response["additionalProperties"] is False
    assert set(error["properties"]) == {
        "code",
        "message",
        "retryable",
        "details",
    }
    assert error["additionalProperties"] is False


def test_ui_runtime_success_payloads_match_documented_models(tmp_path) -> None:
    client, sessions = _ui_app(tmp_path, agent_client=StubChatAgent())
    _seed_baseline(sessions)

    with sessions() as session:
        now = ensure_clock(session).current_time
        notification = Notification(
            patient_id=get_settings().patient_id,
            notification_type="medication_alert",
            title="복약 예정",
            body="복약 시간이 다가옵니다.",
            visible_at=now,
            acknowledged=False,
            metadata_json=json.dumps({"severity": "reminder"}),
        )
        session.add(notification)
        session.commit()
        notification_id = notification.public_id
        schedule_date = now.date().isoformat()

    UiDashboardResponse.model_validate(
        client.get("/api/ui/v1/dashboard").json()
    )
    UiMedicationScenarioListResponse.model_validate(
        client.get("/api/ui/v1/medication-scenarios").json()
    )
    UiNutritionResponse.model_validate(
        client.get("/api/ui/v1/nutrition").json()
    )
    UiPoliciesResponse.model_validate(
        client.get("/api/ui/v1/policies").json()
    )
    UiNotificationListResponse.model_validate(
        client.get("/api/ui/v1/notifications").json()
    )
    UiNotificationDetailResponse.model_validate(
        client.get(f"/api/ui/v1/notifications/{notification_id}").json()
    )
    UiChatHistoryResponse.model_validate(
        client.get("/api/ui/v1/chat/history").json()
    )

    scenario = client.post(
        "/api/ui/v1/medication-scenarios/apply",
        json={
            "request_id": "req_0000000000000110",
            "scenario_id": "tb-diabetes-v1",
            "schedule_date": schedule_date,
        },
    )
    scenario_payload = UiMedicationScenarioApplyResponse.model_validate(
        scenario.json()
    )
    dose_event_id = scenario_payload.data.medications[0].dose_event_id
    UiDoseResponse.model_validate(
        client.post(
            f"/api/ui/v1/doses/{dose_event_id}/take",
            json={},
        ).json()
    )
    UiClockResponse.model_validate(
        client.post("/api/ui/v1/clock/pause", json={}).json()
    )
    UiPoliciesResponse.model_validate(
        client.put(
            "/api/ui/v1/policies/medication_schedule_alert",
            json={"enabled": False},
        ).json()
    )
    UiChatSyncResponse.model_validate(
        client.post(
            "/api/ui/v1/chat/sync",
            json={
                "message": "오늘 약은 언제 먹어?",
                "requested_return_type": "text",
                "request_id": "req_0000000000000111",
            },
        ).json()
    )
    UiNotificationAcknowledgementResponse.model_validate(
        client.post("/api/ui/v1/notifications/ack-all", json={}).json()
    )


def test_ui_runtime_rejects_invalid_dashboard_success_payload(
    monkeypatch,
) -> None:
    def invalid_dashboard_view(_session, *, target_date=None):
        return {
            "internal_patient_id": "must-not-leak",
            "target_date": target_date,
        }

    monkeypatch.setattr(
        "system_app.routes.ui_api.dashboard_view",
        invalid_dashboard_view,
    )
    app = system_main.create_app()

    def session_override():
        yield object()

    app.dependency_overrides[get_session] = session_override
    response = TestClient(
        app,
        raise_server_exceptions=False,
    ).get("/api/ui/v1/dashboard")

    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "data": None,
        "error": {
            "code": "BACKEND_PROCESSING_ERROR",
            "message": "The Backend could not complete the UI request.",
            "retryable": True,
            "details": None,
        },
    }
    assert "must-not-leak" not in response.text


def test_ui_idempotency_replay_rejects_corrupted_error_envelopes(
    monkeypatch,
) -> None:
    corrupted_replay = SimpleNamespace(
        status_code=409,
        body={
            "success": False,
            "data": None,
            "error": {
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "Stored error response.",
                "retryable": False,
                "details": None,
            },
            "internal_patient_id": "must-not-leak",
        },
    )
    monkeypatch.setattr(
        "system_app.routes.ui_api.BackendRequestGate.begin",
        lambda *_args, **_kwargs: corrupted_replay,
    )
    monkeypatch.setattr(
        "system_app.routes.ui_api.testbed_reset_is_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "system_app.routes.ui_api.testbed_reset_is_busy",
        lambda _session: False,
    )
    monkeypatch.setattr(
        "system_app.routes.ui_api._require_simulation_ready",
        lambda _session: None,
    )
    runtime = SimpleNamespace(
        write_lock=threading.RLock(),
        agent_client=None,
    )
    app = FastAPI()
    app.include_router(create_ui_api_router(lambda: runtime))
    session = SimpleNamespace(rollback=lambda: None)

    def session_override():
        yield session

    app.dependency_overrides[get_session] = session_override
    client = TestClient(app)
    cases = (
        (
            "/api/ui/v1/medication-scenarios/apply",
            {
                "request_id": "req_0000000000000112",
                "scenario_id": "tb-diabetes-v1",
                "schedule_date": "2026-07-28",
            },
            "The medication scenario could not be applied.",
        ),
        (
            "/api/ui/v1/testbed/reset",
            {
                "request_id": "req_0000000000000113",
                "confirm": True,
            },
            "The testbed could not be reset.",
        ),
        (
            "/api/ui/v1/clock/advance",
            {
                "request_id": "req_0000000000000114",
                "minutes": 30,
            },
            "The simulation clock could not be advanced.",
        ),
    )

    for path, payload, expected_message in cases:
        response = client.post(path, json=payload)

        assert response.status_code == 500
        assert response.json() == {
            "success": False,
            "data": None,
            "error": {
                "code": "BACKEND_PROCESSING_ERROR",
                "message": expected_message,
                "retryable": True,
                "details": None,
            },
        }
        assert "must-not-leak" not in response.text
