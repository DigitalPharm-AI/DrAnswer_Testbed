import json
import threading
import time
from datetime import date, datetime

from fastapi.testclient import TestClient

import system_app.main as system_main
import system_app.routes.notifications as notifications_routes
from shared.schemas import AgentResponse, MissedDoseEventPayload, PhrPatientRegistrationResult, PhrRegisteredMedication
from shared.settings import get_settings
from system_app.db import SessionLocal
from system_app.main import app
from system_app.models import (
    AgentDecisionAudit,
    AgentJob,
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    MissedDoseFlag,
    Notification,
    ReminderPolicy,
    SimulationPatientProfile,
    SystemPolicyOverride,
)
from system_app.services.agent_jobs import FAILED, PENDING, create_agent_job
from system_app.services.agent_response_service import maybe_apply_policy_response
from system_app.services.dose_event_service import mark_dose_taken
from system_app.services.missed_dose_flag_service import activate_missed_dose_flag
from system_app.services.side_effect_reminder_safety import (
    SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
    SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY,
    create_side_effect_reminder_safety_prompt,
    is_reminder_suppressed_after_side_effect,
    set_reminder_suppressed_after_side_effect,
)
from system_app.services.simulation import create_notification, ensure_base_data, ensure_clock


class FakePhrClient:
    def __init__(self) -> None:
        self.register_calls = []
        self.update_calls = []

    async def register_patient(self, medications):
        self.register_calls.append(medications)
        return PhrPatientRegistrationResult(
            phr_patient_key="phr_test_issued_key",
            medications=[PhrRegisteredMedication(item_name=item.item_name, dosage=item.dosage, active=True) for item in medications],
        )

    async def update_patient_medications(self, phr_patient_key: str, medications):
        self.update_calls.append((phr_patient_key, medications))
        return PhrPatientRegistrationResult(
            phr_patient_key=phr_patient_key,
            medications=[PhrRegisteredMedication(item_name=item.item_name, dosage=item.dosage, active=True) for item in medications],
        )


def wait_for_notification_status(notification_id: int, status: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    last_metadata = {}
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            notification = session.get(Notification, notification_id)
            if notification is not None:
                last_metadata = json.loads(notification.metadata_json)
                if last_metadata.get("status") == status:
                    return last_metadata
        time.sleep(0.05)
    raise AssertionError(f"notification {notification_id} did not reach {status}; last={last_metadata}")


def test_index_includes_popup_stack_and_script():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="popup-stack"' in response.text
    assert "/static/notifications.js" in response.text
    assert 'id="notifications-panel"' in response.text
    assert "data-chat-scroll-region" in response.text


def test_medication_panel_shows_phr_registration_status():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(SimulationPatientProfile).delete()
        session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert "PHR 등록 필요" in response.text
    assert "설정 완료 및 PHR 등록" in response.text


def test_phr_register_endpoint_saves_issued_key(monkeypatch):
    client = TestClient(app)
    fake_phr_client = FakePhrClient()
    monkeypatch.setattr(system_main, "phr_client", fake_phr_client)

    with SessionLocal() as session:
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        session.query(SimulationPatientProfile).delete()
        ensure_base_data(session)
        session.commit()

    client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
            "instructions": "PHR 등록 테스트",
        },
    )
    response = client.post("/phr/register")

    with SessionLocal() as session:
        profile = session.query(SimulationPatientProfile).one()
        profile_key = profile.phr_patient_key
        profile_status = profile.sync_status
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        session.query(SimulationPatientProfile).delete()
        session.commit()

    assert response.status_code == 204
    assert profile_key == "phr_test_issued_key"
    assert profile_status == "synced"
    assert fake_phr_client.register_calls
    assert fake_phr_client.register_calls[0][0].item_name == "혈압약"


def test_simulation_controls_wait_for_phr_sync_after_medication_input():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        session.query(SimulationPatientProfile).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 8, 0)
        clock.last_processed_sim_time = datetime(2026, 4, 20, 8, 0)
        session.commit()

    client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
        },
    )
    page = client.get("/")
    advance_response = client.post("/clock/advance", data={"minutes": "30"})

    with SessionLocal() as session:
        clock = ensure_clock(session)
        current_time = clock.current_time
        is_running = clock.is_running
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        session.query(SimulationPatientProfile).delete()
        session.commit()

    assert page.status_code == 200
    assert "복약 정보를 PHR로 등록한 뒤 시뮬레이션을 실행할 수 있습니다." in page.text
    assert '<button type="submit" disabled>30분 진행</button>' in page.text
    assert '<button type="submit" disabled>재생</button>' in page.text
    assert advance_response.status_code == 204
    assert current_time == datetime(2026, 4, 20, 8, 0)
    assert is_running is False


def test_medication_input_detail_is_collapsed_after_successful_registration(monkeypatch):
    client = TestClient(app)
    fake_phr_client = FakePhrClient()
    monkeypatch.setattr(system_main, "phr_client", fake_phr_client)

    with SessionLocal() as session:
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        session.query(SimulationPatientProfile).delete()
        ensure_base_data(session)
        session.commit()

    client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
        },
    )
    client.post("/phr/register")
    response = client.get("/")

    with SessionLocal() as session:
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        session.query(SimulationPatientProfile).delete()
        session.commit()

    assert response.status_code == 200
    assert "PHR key 발급 완료" in response.text
    assert "PHR key: phr_test_iss..." in response.text
    assert '<details class="phr-key-detail">' not in response.text
    assert '<details class="medication-config-detail">' in response.text
    assert "복약 정보 입력 및 수정" in response.text


def test_medication_change_after_phr_sync_marks_profile_needs_sync(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(system_main, "phr_client", FakePhrClient())

    with SessionLocal() as session:
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        session.query(SimulationPatientProfile).delete()
        ensure_base_data(session)
        session.commit()

    client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
        },
    )
    client.post("/phr/register")
    client.post(
        "/medications",
        data={
            "medication_choice": "당뇨약",
            "dosage_choice": "1정",
            "schedule_template": "morning_lunch_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
        },
    )
    page_response = client.get("/")

    with SessionLocal() as session:
        profile = session.query(SimulationPatientProfile).one()
        profile_key = profile.phr_patient_key
        profile_status = profile.sync_status
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        session.query(SimulationPatientProfile).delete()
        session.commit()

    assert profile_key == "phr_test_issued_key"
    assert profile_status == "needs_sync"
    assert "PHR 재동기화 필요" in page_response.text
    assert "PHR 재동기화" in page_response.text


def test_notifications_feed_returns_expected_shape():
    client = TestClient(app)

    response = client.get("/api/notifications/feed?after_id=0")

    assert response.status_code == 200
    payload = response.json()
    assert "notifications" in payload
    assert "last_seen_id" in payload
    assert "current_time" in payload


def test_chat_request_tracking_notifications_are_hidden_from_alert_surfaces():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        create_notification(
            session,
            notification_type="system_policy_request",
            title="에이전트 대화 전송",
            body="에이전트에게 메시지를 보냈습니다: 알림을 조금 더 자주 받고싶어요",
            visible_at=clock.current_time,
            metadata={"category": "agent_conversation", "status": "sent"},
        )
        create_notification(
            session,
            notification_type="medication_alert",
            title="복약 알림",
            body="오늘 복약 시간입니다.",
            visible_at=clock.current_time,
            metadata={},
        )
        session.commit()

    feed_response = client.get("/api/notifications/feed?after_id=0")
    partial_response = client.get("/partials/notifications")
    feed_bodies = [notification["body"] for notification in feed_response.json()["notifications"]]

    with SessionLocal() as session:
        session.query(Notification).delete()
        session.commit()

    assert feed_response.status_code == 200
    assert partial_response.status_code == 200
    assert "오늘 복약 시간입니다." in feed_bodies
    assert "에이전트에게 메시지를 보냈습니다" not in " ".join(feed_bodies)
    assert "오늘 복약 시간입니다." in partial_response.text
    assert "에이전트에게 메시지를 보냈습니다" not in partial_response.text


def test_notification_detail_returns_existing_alert_status_after_feed_seen():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        notification = create_notification(
            session,
            notification_type="conversation_alert",
            title="미복용 AI 알림",
            body="AI 응답이 도착했습니다.",
            visible_at=clock.current_time,
            metadata={"status": "agent_ready", "trace_id": "trace-ready"},
        )
        notification_id = notification.id
        session.commit()

    stale_feed_response = client.get(f"/api/notifications/feed?after_id={notification_id}")
    detail_response = client.get(f"/api/notifications/{notification_id}")

    assert stale_feed_response.status_code == 200
    assert stale_feed_response.json()["notifications"] == []
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()["notification"]
    assert detail_payload["id"] == notification_id
    assert detail_payload["metadata"]["status"] == "agent_ready"
    assert detail_payload["body"] == "AI 응답이 도착했습니다."


def test_notifications_partial_keeps_collapsible_history_when_empty():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        session.commit()

    response = client.get("/partials/notifications")

    assert response.status_code == 200
    assert 'class="notification-history"' in response.text
    assert "알림 이력" in response.text
    assert "0건" in response.text
    assert "현재 표시할 알림이 없습니다." in response.text


def test_notifications_partial_keeps_acknowledged_today_history_and_excludes_other_days():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        create_notification(
            session,
            notification_type="medication_alert",
            title="읽은 오늘 알림",
            body="오늘 읽은 알림도 이력에 남습니다.",
            visible_at=datetime(2026, 4, 20, 8, 0),
            metadata={},
        ).acknowledged = True
        create_notification(
            session,
            notification_type="conversation_alert",
            title="스킵한 오늘 알림",
            body="오늘 스킵한 알림도 이력에 남습니다.",
            visible_at=datetime(2026, 4, 20, 9, 30),
            metadata={"status": "agent_ready"},
        ).acknowledged = True
        create_notification(
            session,
            notification_type="medication_alert",
            title="어제 알림",
            body="다른 날짜 알림은 오늘 이력에 섞이지 않습니다.",
            visible_at=datetime(2026, 4, 19, 8, 0),
            metadata={},
        )
        session.commit()

    response = client.get("/partials/notifications")
    response_text = response.text

    with SessionLocal() as session:
        session.query(Notification).delete()
        session.commit()

    assert response.status_code == 200
    assert "오늘 읽은 알림도 이력에 남습니다." in response_text
    assert "오늘 스킵한 알림도 이력에 남습니다." in response_text
    assert "다른 날짜 알림은 오늘 이력에 섞이지 않습니다." not in response_text
    assert "2건" in response_text


def test_active_policies_partial_uses_js_refresh_and_displays_policy_period():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ReminderPolicy).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        session.add(
            ReminderPolicy(
                patient_id="demo-patient",
                slot_label="아침 08:00",
                extra_reminders=2,
                interval_minutes=10,
                effective_start_date=date(2026, 4, 20),
                effective_end_date=date(2026, 4, 27),
                reason="시스템 요청으로 아침 알림을 강화합니다.",
                source="system_request",
                active=True,
            )
        )
        session.commit()

    response = client.get("/partials/active-policies")
    response_text = response.text

    with SessionLocal() as session:
        session.query(ReminderPolicy).delete()
        session.commit()

    assert response.status_code == 200
    assert 'hx-get="/partials/active-policies"' not in response_text
    assert all("hx-trigger=" not in line for line in response_text.splitlines()[0:6])
    assert "아침 08:00" in response_text
    assert "2회 추가 / 10분 간격" in response_text
    assert "적용 기간: 2026-04-20 ~ 2026-04-27" in response_text
    assert "시스템 요청으로 아침 알림을 강화합니다." in response_text
    assert "daily_pattern_conversation_time" in response_text
    assert "일일 패턴 대화 요청 시간: 08:30" in response_text


def test_active_policies_partial_keeps_same_pattern_policies_separate():
    client = TestClient(app)
    reason = "아침 08:00, 점심 13:00에서 동일한 누락 패턴이 확인되어 같은 설정을 시간대별로 개별 적용합니다."

    with SessionLocal() as session:
        session.query(ReminderPolicy).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        for slot_label in ("아침 08:00", "점심 13:00"):
            session.add(
                ReminderPolicy(
                    patient_id="demo-patient",
                    slot_label=slot_label,
                    extra_reminders=2,
                    interval_minutes=10,
                    effective_start_date=date(2026, 4, 20),
                    effective_end_date=date(2026, 4, 27),
                    reason=reason,
                    source="pattern_analysis",
                    active=True,
                )
            )
        session.commit()

    response = client.get("/partials/active-policies")
    response_text = response.text

    with SessionLocal() as session:
        session.query(ReminderPolicy).delete()
        session.commit()

    assert response.status_code == 200
    assert "아침 08:00 / 점심 13:00" not in response_text
    assert "맞춤 정책 묶음" not in response_text
    assert "아침 08:00" in response_text
    assert "점심 13:00" in response_text
    assert "2회 추가 / 10분 간격" in response_text
    assert response_text.count("2회 추가 / 10분 간격") == 2
    assert response_text.count(reason) == 2


def test_active_policies_partial_can_toggle_all_reminders():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
        ensure_base_data(session)
        session.commit()

    initial_response = client.get("/partials/active-policies")
    off_response = client.post("/reminders/suppression", data={"suppressed": "true"})
    after_off_response = client.get("/partials/active-policies")

    try:
        assert initial_response.status_code == 200
        assert "복약/AI 알림 전체 상태" in initial_response.text
        assert "알림 전체 끄기" in initial_response.text
        assert off_response.status_code == 204
        assert after_off_response.status_code == 200
        assert "알림 전체 켜기" in after_off_response.text
        assert "수동 패턴 분석이 중지되어 있습니다." in after_off_response.text
        with SessionLocal() as session:
            assert is_reminder_suppressed_after_side_effect(session) is True
        on_response = client.post("/reminders/suppression", data={"suppressed": "false"})
        after_on_response = client.get("/partials/active-policies")
        assert on_response.status_code == 204
        assert after_on_response.status_code == 200
        assert "알림 전체 끄기" in after_on_response.text
        with SessionLocal() as session:
            assert is_reminder_suppressed_after_side_effect(session) is False
    finally:
        with SessionLocal() as session:
            session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
            session.commit()


def test_manual_pattern_analysis_route_is_disabled_when_all_reminders_are_suppressed(monkeypatch):
    client = TestClient(app)

    class RecordingAgentClient:
        def __init__(self) -> None:
            self.calls = 0

        async def send_daily_pattern_async(self, payload):
            self.calls += 1
            return AgentResponse(
                trace_id="trace-manual-suppressed",
                agent_name="daily_pattern_agent",
                prompt_version_id="daily_pattern_agent_v1",
                decision_type="pattern_analysis",
                structured_payload={},
                human_summary="호출되면 안 됩니다.",
            )

    recording_client = RecordingAgentClient()
    monkeypatch.setattr(system_main, "agent_client", recording_client)

    with SessionLocal() as session:
        session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
        ensure_base_data(session)
        set_reminder_suppressed_after_side_effect(session, True, reason="테스트")
        session.commit()

    response = client.post("/analysis/run-today")
    clock_response = client.get("/")

    try:
        assert response.status_code == 204
        assert recording_client.calls == 0
        assert "오늘 패턴 분석</button>" in clock_response.text
        assert '<button type="submit" disabled>오늘 패턴 분석</button>' in clock_response.text
    finally:
        with SessionLocal() as session:
            session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
            session.commit()


def test_simulation_reset_clears_all_reminder_suppression():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
        ensure_base_data(session)
        set_reminder_suppressed_after_side_effect(session, True, reason="테스트")
        session.commit()

    response = client.post("/simulation/reset")

    try:
        assert response.status_code == 204
        with SessionLocal() as session:
            assert is_reminder_suppressed_after_side_effect(session) is False
    finally:
        with SessionLocal() as session:
            session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
            session.commit()


def test_system_request_history_partial_displays_sent_and_applied_statuses():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        create_notification(
            session,
            notification_type="system_policy_request",
            title="시스템 정책 반영 완료",
            body="정책 요청이 반영되었습니다: 아침 알림을 2번 10분 간격으로 바꿔줘",
            visible_at=clock.current_time,
            metadata={
                "status": "applied",
                "request_message": "아침 알림을 2번 10분 간격으로 바꿔줘",
                "result_message": "정책이 적용되었습니다.",
            },
        )
        session.commit()

    response = client.get("/partials/system-request-history")
    response_text = response.text

    with SessionLocal() as session:
        session.query(Notification).delete()
        session.commit()

    assert response.status_code == 200
    assert "아침 알림을 2번 10분 간격으로 바꿔줘" in response_text
    assert "전송됨" in response_text
    assert "반영됨" in response_text
    assert "정책이 적용되었습니다." in response_text


def test_agent_policy_apply_api_rejects_direct_application():
    client = TestClient(app)
    notification_payload = {
        "idempotency_key": "test-policy-tool-key",
        "source_trace_id": "trace-policy-tool",
        "source_event_type": "daily_pattern",
        "policies": [
            {
                "slot_label": "아침 08:00",
                "extra_reminders": 2,
                "interval_minutes": 5,
                "effective_start_date": "2026-04-20",
                "effective_end_date": "2026-04-27",
                "reason": "아침 누락률이 높습니다.",
                "source": "pattern_analysis",
            },
            {
                "slot_label": "점심 13:00",
                "extra_reminders": 2,
                "interval_minutes": 10,
                "effective_start_date": "2026-04-20",
                "effective_end_date": "2026-04-27",
                "reason": "점심 누락률이 높습니다.",
                "source": "pattern_analysis",
            },
        ],
    }
    system_payload = {
        "idempotency_key": "test-system-policy-tool-key",
        "source_trace_id": "trace-system-policy-tool",
        "source_event_type": "multiturn_chat",
        "policies": [
            {
                "policy_key": "daily_pattern_conversation_time",
                "value": "07:30",
                "reason": "아침에 먼저 확인하기 위한 후보입니다.",
            }
        ],
    }

    with SessionLocal() as session:
        session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id.in_([notification_payload["idempotency_key"], system_payload["idempotency_key"]])).delete()
        session.query(ReminderPolicy).delete()
        session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == "daily_pattern_conversation_time").delete()
        ensure_base_data(session)
        session.commit()

    notification_response = client.post("/api/agent/policies/apply", json=notification_payload)
    system_response = client.post("/api/agent/system-policies/apply", json=system_payload)

    with SessionLocal() as session:
        policies = session.query(ReminderPolicy).filter(ReminderPolicy.source == "pattern_analysis").order_by(ReminderPolicy.slot_label).all()
        audits = session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id.in_([notification_payload["idempotency_key"], system_payload["idempotency_key"]])).all()
        overrides = session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == "daily_pattern_conversation_time").all()
        session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id.in_([notification_payload["idempotency_key"], system_payload["idempotency_key"]])).delete()
        session.query(ReminderPolicy).delete()
        session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == "daily_pattern_conversation_time").delete()
        session.commit()

    assert notification_response.status_code == 410
    assert notification_response.json()["detail"] == "policy_apply_requires_confirmation"
    assert system_response.status_code == 410
    assert system_response.json()["detail"] == "system_policy_apply_requires_confirmation"
    assert policies == []
    assert overrides == []
    assert audits == []


def test_agent_notification_callback_creates_notification_once_with_idempotency():
    client = TestClient(app)
    payload = {
        "idempotency_key": "test-notification-callback-key",
        "notification_type": "conversation_alert",
        "title": "부작용 확인 결과",
        "body": "부작용 가능성이 있어 추가 확인이 필요합니다.",
        "metadata": {"status": "agent_ready", "category": "side_effect"},
        "chat_category": "side_effect",
    }

    with SessionLocal() as session:
        session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == payload["idempotency_key"]).delete()
        session.query(ChatMessage).filter(ChatMessage.category == "side_effect").delete()
        session.query(Notification).filter(Notification.title == payload["title"]).delete()
        ensure_base_data(session)
        session.commit()

    first_response = client.post("/api/agent/notifications", json=payload)
    second_response = client.post("/api/agent/notifications", json=payload)

    with SessionLocal() as session:
        notifications = session.query(Notification).filter(Notification.title == payload["title"]).all()
        messages = session.query(ChatMessage).filter(ChatMessage.category == "side_effect").all()
        session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == payload["idempotency_key"]).delete()
        session.query(ChatMessage).filter(ChatMessage.category == "side_effect").delete()
        session.query(Notification).filter(Notification.title == payload["title"]).delete()
        session.commit()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["notification_id"] == second_response.json()["notification_id"]
    assert len(notifications) == 1
    assert len(messages) == 1


def test_internal_agent_api_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_api_token", "test-internal-token")
    client = TestClient(app)

    blocked_response = client.post("/admin/policies/reload")
    allowed_response = client.post(
        "/admin/policies/reload",
        headers={"X-Internal-Api-Token": "test-internal-token"},
    )

    assert blocked_response.status_code == 401
    assert blocked_response.json()["detail"] == "invalid_internal_api_token"
    assert allowed_response.status_code == 200


def test_system_chat_records_sent_request_before_background_completion(monkeypatch):
    client = TestClient(app)
    worker_called = threading.Event()
    worker_args = []

    def fake_system_event_worker(event_type: str, message: str, notification_id: int) -> None:
        worker_args.append((event_type, message, notification_id))
        worker_called.set()

    monkeypatch.setattr(system_main, "system_event_worker", fake_system_event_worker)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(MissedDoseFlag).delete()
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        ensure_base_data(session)
        session.commit()

    response = client.post(
        "/chat/system",
        data={"event_type": "multiturn_chat", "message": "아침 알림을 2번 10분 간격으로 바꿔줘"},
    )

    assert response.status_code == 200
    assert "chat-log-region" in response.text
    assert "아침 알림을 2번 10분 간격으로 바꿔줘" in response.text
    assert "HX-Refresh" not in response.headers
    assert worker_called.wait(timeout=2)

    with SessionLocal() as session:
        notification = session.query(Notification).filter(Notification.notification_type == "system_policy_request").one()
        chat_message = session.query(ChatMessage).filter(ChatMessage.category == "multiturn_chat").one()
        metadata = json.loads(notification.metadata_json)
        notification_id = notification.id

        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.commit()

    assert worker_args == [("multiturn_chat", "아침 알림을 2번 10분 간격으로 바꿔줘", notification_id)]
    assert notification.title == "에이전트 대화 전송"
    assert "에이전트에게 메시지를 보냈습니다" in notification.body
    assert metadata["status"] == "sent"
    assert metadata["category"] == "agent_conversation"
    assert metadata["request_message"] == "아침 알림을 2번 10분 간격으로 바꿔줘"
    assert chat_message.content == "아침 알림을 2번 10분 간격으로 바꿔줘"
    assert chat_message.sender_type == "patient"


def test_missed_dose_prompt_reply_uses_multiturn_chat_route(monkeypatch):
    client = TestClient(app)
    worker_called = threading.Event()
    worker_args = []

    def fake_system_event_worker(event_type: str, message: str, notification_id: int) -> None:
        worker_args.append((event_type, message, notification_id))
        worker_called.set()

    monkeypatch.setattr(system_main, "system_event_worker", fake_system_event_worker)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(MissedDoseFlag).delete()
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.is_running = False
        clock.speed_multiplier = 0
        plan = MedicationPlan(
            patient_id="demo-patient",
            medication_name="당뇨약",
            dosage="1정",
            start_date=clock.current_time.date(),
            end_date=clock.current_time.date(),
            active=True,
        )
        session.add(plan)
        session.flush()
        schedule = DoseSchedule(plan_id=plan.id, slot_label="아침 08:00", scheduled_time="08:00")
        session.add(schedule)
        session.flush()
        event = DoseEvent(
            patient_id="demo-patient",
            plan_id=plan.id,
            schedule_id=schedule.id,
            medication_name="당뇨약",
            slot_label="아침 08:00",
            scheduled_for=clock.current_time.replace(hour=8, minute=0, second=0, microsecond=0),
            status="missed",
            missed_detected_at=clock.current_time,
            missed_handled=True,
        )
        session.add(event)
        session.flush()
        activate_missed_dose_flag(session, event, activated_at=clock.current_time)
        prompt = create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="아침 08:00 당뇨약을 놓친 이유를 알려주세요.",
            visible_at=clock.current_time,
            related_dose_event_id=event.id,
            metadata={
                "category": "missed_dose",
                "status": "agent_ready",
                "resume_clock": {"was_running": True, "speed_multiplier": 12},
            },
        )
        prompt_id = prompt.id
        session.commit()

    response = client.post(
        "/chat/system",
        data={"event_type": "multiturn_chat", "message": "속이 메스꺼운데 약때문일까?"},
    )

    assert response.status_code == 200
    assert worker_called.wait(timeout=2)
    with SessionLocal() as session:
        prompt = session.get(Notification, prompt_id)
        clock = ensure_clock(session)
        assert prompt is not None
        assert prompt.acknowledged is True
        assert clock.is_running is False
        assert clock.speed_multiplier == 0
        assert session.query(ChatMessage).filter(ChatMessage.category == "reply").count() == 0
        assert session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one().content == "아침 08:00 당뇨약을 놓친 이유를 알려주세요."
        user_message = session.query(ChatMessage).filter(ChatMessage.category == "multiturn_chat", ChatMessage.sender_type == "patient").one()
        system_notification = session.query(Notification).filter(Notification.notification_type == "system_policy_request").one()
        prompt_metadata = json.loads(prompt.metadata_json)
        user_metadata = json.loads(user_message.metadata_json)
        system_metadata = json.loads(system_notification.metadata_json)
        system_notification_id = system_notification.id

        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.commit()

    assert user_message.content == "속이 메스꺼운데 약때문일까?"
    assert prompt_metadata["patient_reply"] == "속이 메스꺼운데 약때문일까?"
    assert prompt_metadata["missed_dose_reply_understanding"]["barrier_type"] == "side_effect_concern"
    assert prompt_metadata["missed_dose_reply_understanding"]["policy_signals"]["needs_side_effect_check"] is True
    assert user_metadata["missed_dose_reply"]["understanding"]["barrier_type"] == "side_effect_concern"
    assert system_metadata["missed_dose_reply"]["understanding"]["policy_signals"]["prefer_tone"] == "side_effect_check"
    assert worker_args == [("multiturn_chat", "속이 메스꺼운데 약때문일까?", system_notification_id)]


def test_system_chat_does_not_attach_to_stale_missed_dose_alert_after_later_taken(monkeypatch):
    client = TestClient(app)
    worker_called = threading.Event()
    worker_args = []

    def fake_system_event_worker(event_type: str, message: str, notification_id: int) -> None:
        worker_args.append((event_type, message, notification_id))
        worker_called.set()

    monkeypatch.setattr(system_main, "system_event_worker", fake_system_event_worker)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(MissedDoseFlag).delete()
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.is_running = False
        clock.speed_multiplier = 0
        plan = MedicationPlan(
            patient_id="demo-patient",
            medication_name="당뇨약",
            dosage="1정",
            start_date=clock.current_time.date(),
            end_date=clock.current_time.date(),
            active=True,
        )
        session.add(plan)
        session.flush()
        morning = DoseSchedule(plan_id=plan.id, slot_label="아침 08:00", scheduled_time="08:00")
        lunch = DoseSchedule(plan_id=plan.id, slot_label="점심 12:00", scheduled_time="12:00")
        session.add_all([morning, lunch])
        session.flush()
        morning_event = DoseEvent(
            patient_id="demo-patient",
            plan_id=plan.id,
            schedule_id=morning.id,
            medication_name="당뇨약",
            slot_label="아침 08:00",
            scheduled_for=clock.current_time.replace(hour=8, minute=0, second=0, microsecond=0),
            status="missed",
            missed_detected_at=clock.current_time,
            missed_handled=True,
        )
        lunch_event = DoseEvent(
            patient_id="demo-patient",
            plan_id=plan.id,
            schedule_id=lunch.id,
            medication_name="당뇨약",
            slot_label="점심 12:00",
            scheduled_for=clock.current_time.replace(hour=12, minute=0, second=0, microsecond=0),
            status="scheduled",
        )
        session.add_all([morning_event, lunch_event])
        session.flush()
        activate_missed_dose_flag(session, morning_event, activated_at=clock.current_time)
        prompt = create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="아침 08:00 당뇨약을 놓친 이유를 알려주세요.",
            visible_at=clock.current_time,
            related_dose_event_id=morning_event.id,
            metadata={
                "category": "missed_dose",
                "status": "agent_ready",
                "resume_clock": {"was_running": True, "speed_multiplier": 12},
            },
        )
        prompt_id = prompt.id
        lunch_event_id = lunch_event.id
        session.commit()

    with SessionLocal() as session:
        mark_dose_taken(session, lunch_event_id, taken_at=datetime(2026, 4, 20, 12, 5))

    response = client.post(
        "/chat/system",
        data={"event_type": "multiturn_chat", "message": "아까 무슨 얘기했지?"},
    )

    assert response.status_code == 200
    assert worker_called.wait(timeout=2)
    with SessionLocal() as session:
        prompt = session.get(Notification, prompt_id)
        prompt_metadata = json.loads(prompt.metadata_json)
        user_message = session.query(ChatMessage).filter(ChatMessage.category == "multiturn_chat", ChatMessage.sender_type == "patient").one()
        user_metadata = json.loads(user_message.metadata_json)
        system_notification = session.query(Notification).filter(Notification.notification_type == "system_policy_request").one()
        system_notification_id = system_notification.id

        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.commit()

    assert prompt.acknowledged is False
    assert "patient_reply" not in prompt_metadata
    assert "missed_dose_reply" not in user_metadata
    assert worker_args == [("multiturn_chat", "아까 무슨 얘기했지?", system_notification_id)]


def test_system_chat_partial_reflects_background_completion_without_restart(monkeypatch):
    client = TestClient(app)
    release_worker = threading.Event()

    def fake_system_event_worker(event_type: str, message: str, notification_id: int) -> None:
        from system_app.services.system_request_service import apply_system_event_response

        release_worker.wait(timeout=2)
        response = AgentResponse(
            trace_id="trace-system-complete",
            agent_name="system_event_agent",
            prompt_version_id="system_event_agent_v11",
            decision_type="side_effect_assessment",
            structured_payload={"advice": "백그라운드 완료 답변입니다."},
            human_summary="백그라운드 완료 답변입니다.",
            requires_conversation_alert=False,
        )
        with SessionLocal() as worker_session:
            apply_system_event_response(worker_session, event_type, message, notification_id, response)
            worker_session.commit()

    monkeypatch.setattr(system_main, "system_event_worker", fake_system_event_worker)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(MissedDoseFlag).delete()
        session.query(DoseEvent).delete()
        session.query(DoseSchedule).delete()
        session.query(MedicationPlan).delete()
        ensure_base_data(session)
        session.commit()

    response = client.post(
        "/chat/system",
        data={"event_type": "multiturn_chat", "message": "속이 메스꺼운데 약때문일까?"},
    )
    assert response.status_code == 200
    assert "속이 메스꺼운데 약때문일까?" in response.text
    assert "응답 생성 중" in response.text

    with SessionLocal() as session:
        notification_id = session.query(Notification).filter(Notification.notification_type == "system_policy_request").one().id

    release_worker.set()
    wait_for_notification_status(notification_id, "answered")
    partial_response = client.get("/partials/chat-log")

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.commit()

    assert partial_response.status_code == 200
    assert "백그라운드 완료 답변입니다." in partial_response.text
    assert "응답 생성 중" not in partial_response.text


def test_notification_ack_marks_notification_read():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        notification = create_notification(
            session,
            notification_type="conversation_alert",
            title="테스트 응답 알림",
            body="응답하거나 스킵할 수 있습니다.",
            visible_at=clock.current_time,
        )
        notification_id = notification.id
        session.commit()

    response = client.post(f"/notifications/{notification_id}/ack")

    assert response.status_code == 204

    with SessionLocal() as session:
        refreshed = session.get(Notification, notification_id)
        assert refreshed is not None
        assert refreshed.acknowledged is True


def test_conversation_alert_partial_routes_replies_to_chat():
    client = TestClient(app)

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        create_notification(
            session,
            notification_type="conversation_alert",
            title="테스트 답변 알림",
            body="AI가 복약 이유를 물었습니다.",
            visible_at=clock.current_time,
            metadata={"category": "missed_dose", "status": "agent_ready"},
        )
        session.commit()

    response = client.get("/partials/notifications")

    assert response.status_code == 200
    assert "AI가 복약 이유를 물었습니다." in response.text
    assert 'class="notification-history"' in response.text
    assert "알림 이력" in response.text
    assert "/reply" not in response.text
    assert "/chat/" + "patient" not in response.text
    assert "채팅 열기" in response.text
    assert "data-open-chat" in response.text
    assert "답변 보내기" not in response.text


def test_pending_conversation_alert_waits_for_agent_before_reply_form():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        create_notification(
            session,
            notification_type="conversation_alert",
            title="미복용 AI 알림",
            body="AI가 상황을 확인하고 있어요.",
            visible_at=clock.current_time,
            metadata={"status": "awaiting_agent"},
        )
        session.commit()

    response = client.get("/partials/notifications")

    assert response.status_code == 200
    assert "AI 처리 중" in response.text
    assert "agent가 확인하고 있습니다" in response.text
    assert "progress-round" in response.text
    assert "alert-agent-progress" in response.text
    assert "답변 보내기" not in response.text


def test_submitted_conversation_alert_shows_locked_reply_and_progress():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        create_notification(
            session,
            notification_type="conversation_alert",
            title="미복용 AI 알림",
            body="답변을 처리하고 있습니다.",
            visible_at=clock.current_time,
            metadata={"status": "reply_submitted", "patient_reply": "깜빡했어요."},
        )
        session.commit()

    response = client.get("/partials/notifications")

    assert response.status_code == 200
    assert "제출한 답변" in response.text
    assert "깜빡했어요." in response.text
    assert "readonly" in response.text
    assert "AI 처리 중" in response.text
    assert "progress-round" in response.text
    assert "답변 보내기" not in response.text


def test_failed_conversation_alert_does_not_show_reply_form():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        create_notification(
            session,
            notification_type="conversation_alert",
            title="미복용 AI 알림",
            body="AI가 미복용 상황을 처리하지 못했습니다.",
            visible_at=clock.current_time,
            metadata={"status": "agent_error", "agent_job_id": 42},
        )
        session.commit()

    response = client.get("/partials/notifications")

    assert response.status_code == 200
    assert "AI 처리 실패" in response.text
    assert "다시 시도할 수 있습니다" in response.text
    assert "is-error" in response.text
    assert "답변 보내기" not in response.text


def test_pro_ctcae_completion_creates_side_effect_safety_prompt_once():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
        ensure_base_data(session)
        prompt = ChatMessage(
            patient_id="demo-patient",
            role="assistant",
            sender_type="assistant",
            category="ae_pro_ctcae",
            content="증상에 맞는 PRO-CTCAE 자기보고 문항을 준비했습니다.",
            metadata_json=json.dumps(
                {
                    "ae_pro_ctcae": {
                        "input_symptom": "속이 메스꺼운데 약 때문일까?",
                        "matched": True,
                        "match_type": "exact",
                        "matched_korean_symptom_name": "메스꺼움",
                        "questions": [
                            {"question": "지난 일주일 동안, 메스꺼움을 얼마나 자주 느꼈습니까?", "response_options": ["자주 있다"]},
                            {"question": "지난 일주일 동안, 메스꺼움이 가장 심할 때는 어느 정도였습니까?", "response_options": ["보통이다"]},
                        ],
                        "responses": [],
                    }
                },
                ensure_ascii=False,
            ),
        )
        session.add(prompt)
        session.commit()
        prompt_id = prompt.id

    first_response = client.post(
        "/chat/ae-response",
        data={"chat_message_id": prompt_id, "question_index": "0", "response_text": "자주 있다"},
    )
    complete_response = client.post(
        "/chat/ae-response",
        data={"chat_message_id": prompt_id, "question_index": "1", "response_text": "보통이다"},
    )
    duplicate_response = client.post(
        "/chat/ae-response",
        data={"chat_message_id": prompt_id, "question_index": "1", "response_text": "보통이다"},
    )

    try:
        assert first_response.status_code == 200
        assert complete_response.status_code == 200
        assert duplicate_response.status_code == 200
        assert "부작용에 대해 기록했습니다." in complete_response.text
        assert "알림 유지하기" in complete_response.text
        assert "알림 모두 끄기" in complete_response.text
        assert 'action="/chat/side-effect-reminder-safety"' in complete_response.text
        with SessionLocal() as session:
            prompts = [
                row
                for row in session.query(Notification).filter(Notification.notification_type == "conversation_alert").all()
                if json.loads(row.metadata_json).get("category") == SIDE_EFFECT_REMINDER_SAFETY_CATEGORY
            ]
            assert len(prompts) == 1
            metadata = json.loads(prompts[0].metadata_json)
            assert metadata["source_chat_message_id"] == prompt_id
            assert metadata["status"] == "agent_ready"
            assert [option["label"] for option in metadata["options"]] == ["알림 유지하기", "알림 모두 끄기"]
            assert (
                session.query(ChatMessage)
                .filter(ChatMessage.category == SIDE_EFFECT_REMINDER_SAFETY_CATEGORY, ChatMessage.content.contains("부작용에 대해 기록했습니다."))
                .count()
                == 1
            )
    finally:
        with SessionLocal() as session:
            session.query(ChatMessage).delete()
            session.query(Notification).delete()
            session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
            session.commit()


def test_side_effect_safety_reply_keep_and_suppress_update_override():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
        ensure_base_data(session)
        keep_prompt = create_side_effect_reminder_safety_prompt(session, source_chat_message_id=101)
        suppress_prompt = create_side_effect_reminder_safety_prompt(session, source_chat_message_id=102)
        keep_id = keep_prompt.id
        suppress_id = suppress_prompt.id
        session.commit()

    keep_response = client.post(
        "/chat/side-effect-reminder-safety",
        data={"notification_id": keep_id, "action": "keep"},
    )
    suppress_response = client.post(
        "/chat/side-effect-reminder-safety",
        data={"notification_id": suppress_id, "action": "suppress"},
    )
    duplicate_suppress_response = client.post(
        "/chat/side-effect-reminder-safety",
        data={"notification_id": suppress_id, "action": "suppress"},
    )

    try:
        assert keep_response.status_code == 200
        assert "기존 복약 알림과 미복용 AI 알림을 유지합니다." in keep_response.text
        assert suppress_response.status_code == 200
        assert duplicate_suppress_response.status_code == 200
        assert "복약 알림과 미복용 AI 알림을 모두 껐습니다." in suppress_response.text
        assert "이미 처리된 알림입니다." in duplicate_suppress_response.text
        assert 'action="/chat/side-effect-reminder-safety"' not in suppress_response.text
        with SessionLocal() as session:
            keep_notification = session.get(Notification, keep_id)
            suppress_notification = session.get(Notification, suppress_id)
            override = (
                session.query(SystemPolicyOverride)
                .filter(
                    SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY,
                    SystemPolicyOverride.active.is_(True),
                )
                .one()
            )
            assert keep_notification is not None
            assert suppress_notification is not None
            assert keep_notification.acknowledged is True
            assert suppress_notification.acknowledged is True
            assert json.loads(keep_notification.metadata_json)["patient_reply"] == "알림 유지하기"
            assert json.loads(suppress_notification.metadata_json)["patient_reply"] == "알림 모두 끄기"
            assert override.value == "true"
            prompt_messages = (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.category == SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
                    ChatMessage.content.contains("부작용에 대해 기록했습니다."),
                )
                .all()
            )
            assert len(prompt_messages) == 2
            for prompt_message in prompt_messages:
                prompt_metadata = json.loads(prompt_message.metadata_json)
                assert prompt_metadata["conversation_alert"]["status"] == "reply_completed"
                assert prompt_metadata[SIDE_EFFECT_REMINDER_SAFETY_CATEGORY]["status"] == "reply_completed"
            assert (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.category == SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
                    ChatMessage.content == "복약 알림과 미복용 AI 알림을 모두 껐습니다. 복약 재개나 조정은 의료진과 상담 후 다시 설정해주세요.",
                )
                .count()
                == 1
            )
            assert (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.category == SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
                    ChatMessage.content == "이미 처리된 알림입니다.",
                )
                .count()
                == 1
            )
    finally:
        with SessionLocal() as session:
            session.query(ChatMessage).delete()
            session.query(Notification).delete()
            session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY).delete()
            session.commit()


def test_policy_confirmation_reply_keeps_success_result_until_confirmed():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(ReminderPolicy).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.is_running = True
        clock.speed_multiplier = 12
        response = AgentResponse(
            trace_id="trace-policy-confirm-ui",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v4",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "propose_notification_policy",
                    "arguments": {
                        "slot_label": "아침 08:00",
                        "extra_reminders": 2,
                        "interval_minutes": 10,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "아침 알림 강화 테스트",
                        "source": "pattern_analysis",
                    },
                }
            },
            human_summary="아침 알림을 늘려보는 게 좋겠습니다.",
            requires_conversation_alert=False,
        )
        maybe_apply_policy_response(session, response, "daily_pattern")
        notification = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        notification_id = notification.id
        session.commit()

    partial_response = client.get("/partials/chat-log")
    assert partial_response.status_code == 200
    assert "policy-change-table" in partial_response.text
    assert "현재" in partial_response.text
    assert "변경 후" in partial_response.text
    assert "아침 08:00" in partial_response.text
    assert "추가 2회 · 10분 간격 · 미복용 90분 후 · 정시 알림" in partial_response.text
    assert "policy-option-number" in partial_response.text
    assert "늘리기" in partial_response.text
    assert "현행 유지하기" in partial_response.text
    assert "제안하겠습니다" not in partial_response.text
    notification_response = client.get("/partials/notifications")
    feed_response = client.get("/api/notifications/feed?after_id=0")
    assert notification_response.status_code == 200
    assert "AI가 대화를 요청합니다." in notification_response.text
    assert "아침 08:00" in notification_response.text
    assert "채팅 열기" in notification_response.text
    assert "data-open-chat" in notification_response.text
    feed_notifications = feed_response.json()["notifications"]
    policy_feed = next(row for row in feed_notifications if row["id"] == notification_id)
    assert policy_feed["title"] == "AI가 대화를 요청합니다."
    assert policy_feed["metadata"]["category"] == "policy_confirmation"
    assert "suppress_popup" not in policy_feed["metadata"]
    assert "delivery_channel" not in policy_feed["metadata"]

    response = client.post(
        "/chat/policy-confirmation",
        data={"notification_id": notification_id, "message": json.dumps({"prompt_type": "policy_confirmation", "action": "increase", "number": 1, "label": "늘리기"})},
    )

    assert response.status_code == 200
    metadata = wait_for_notification_status(notification_id, "reply_completed")
    with SessionLocal() as session:
        refreshed = session.get(Notification, notification_id)
        clock = ensure_clock(session)
        assert refreshed is not None
        assert refreshed.acknowledged is True
        assert clock.is_running is True
        assert clock.speed_multiplier == 12
        assert metadata["status"] == "reply_completed"
        assert metadata["agent_reply"] == "아침 08:00: 정책이 적용되었습니다."
        policy = session.query(ReminderPolicy).filter(ReminderPolicy.source == "pattern_analysis").one()
        assert policy.extra_reminders == 2

    partial_response = client.get("/partials/chat-log")
    assert partial_response.status_code == 200
    assert "아침 08:00: 정책이 적용되었습니다." in partial_response.text
    with SessionLocal() as session:
        assert (
            session.query(ChatMessage)
            .filter(ChatMessage.role == "user", ChatMessage.category == "reply", ChatMessage.content.contains("policy_confirmation"))
            .count()
            == 0
        )
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(ReminderPolicy).delete()
        session.commit()


def test_policy_confirmation_prompt_appears_in_chat_from_notification_metadata():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(ReminderPolicy).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        response = AgentResponse(
            trace_id="trace-policy-fallback-ui",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v4",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "propose_notification_policy",
                    "arguments": {
                        "slot_label": "아침 08:00",
                        "extra_reminders": 2,
                        "interval_minutes": 10,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "아침 알림 강화 테스트",
                        "source": "pattern_analysis",
                    },
                }
            },
            human_summary="아침 알림을 늘려보는 게 좋겠습니다.",
            requires_conversation_alert=False,
        )
        maybe_apply_policy_response(session, response, "daily_pattern")
        notification = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        notification_id = notification.id
        session.query(ChatMessage).delete()
        session.commit()

    partial_response = client.get("/partials/chat-log")

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(ReminderPolicy).delete()
        session.commit()

    assert partial_response.status_code == 200
    assert f'name="notification_id" value="{notification_id}"' in partial_response.text
    assert "policy-change-table" in partial_response.text
    assert "아침 08:00" in partial_response.text
    assert "늘리기" in partial_response.text
    assert "현행 유지하기" in partial_response.text


def test_policy_confirmation_decrease_prompt_shows_decrease_choice_in_chat():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(ReminderPolicy).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        session.add(
            ReminderPolicy(
                patient_id="demo-patient",
                slot_label="아침 08:00",
                extra_reminders=3,
                interval_minutes=5,
                effective_start_date=date(2026, 4, 1),
                effective_end_date=date(2026, 4, 30),
                reason="기존 강화 정책",
                source="system_request",
                active=True,
            )
        )
        session.commit()
        response = AgentResponse(
            trace_id="trace-policy-decrease-ui",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v4",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "propose_notification_policy",
                    "arguments": {
                        "slot_label": "아침 08:00",
                        "extra_reminders": 1,
                        "interval_minutes": 20,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "알림 부담을 줄입니다.",
                        "source": "pattern_analysis",
                    },
                }
            },
            human_summary="아침 알림을 줄여보는 게 좋겠습니다.",
            requires_conversation_alert=False,
        )
        maybe_apply_policy_response(session, response, "daily_pattern")
        session.commit()

    partial_response = client.get("/partials/chat-log")

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        session.query(ReminderPolicy).delete()
        session.commit()

    assert partial_response.status_code == 200
    assert "줄이기" in partial_response.text
    assert "현행 유지하기" in partial_response.text
    assert "늘리기" not in partial_response.text


def test_agent_error_notification_can_retry_failed_job(monkeypatch):
    client = TestClient(app)
    retry_calls = []

    async def fake_post_agent_task_action(request_id: str, action: str, reason: str = ""):
        retry_calls.append((request_id, action, reason))
        return {"success": True}

    monkeypatch.setattr(notifications_routes, "post_agent_task_action", fake_post_agent_task_action)

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        job = create_agent_job(
            session,
            "missed_dose",
            MissedDoseEventPayload(
                patient_id="demo-patient",
                dose_event_id=999,
                medication_name="테스트약",
                slot_label="아침 08:00",
                scheduled_for=datetime(2026, 4, 20, 8, 0),
                detected_at=datetime(2026, 4, 20, 8, 40),
            ),
        )
        job.status = FAILED
        notification = create_notification(
            session,
            notification_type="agent_error",
            title="AI 에이전트 오류",
            body="다시 시도할 수 있습니다.",
            visible_at=clock.current_time,
            metadata={"agent_job_id": job.id, "request_id": "missed_dose:conversation:retry-test"},
        )
        job_id = job.id
        notification_id = notification.id
        session.commit()

    response = client.post(f"/agent-jobs/{job_id}/retry?notification_id={notification_id}")

    assert response.status_code == 204
    with SessionLocal() as session:
        refreshed_job = session.get(AgentJob, job_id)
        refreshed_notification = session.get(Notification, notification_id)
        assert refreshed_job is not None
        assert refreshed_job.status == PENDING
        assert refreshed_notification is not None
        assert refreshed_notification.acknowledged is True
    assert retry_calls == [
        ("missed_dose:conversation:retry-test", "retry", "retry from agent error notification")
    ]
