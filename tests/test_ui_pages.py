import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from shared.time_utils import utc_now
from system_app.db import SessionLocal
from system_app.main import app
from system_app.models import AgentDecisionAudit, AgentRunStep, AgentRunTrace, ChatMessage, DoseEvent, Notification
from system_app.services.clock_service import ensure_clock
from system_app.services.dashboard_view import chat_message_view, sorted_chat_views
from system_app.services.notification_service import create_notification
from system_app.services.patient_profile_service import ensure_base_data
from system_app.services.timeline_service import add_chat_message


LOGS_SECTION_MARKERS = (
    "비동기 연동 상태",
    "Containment Action",
    "Agent workers",
    "최근 local jobs",
    "Agent Alerts",
    "Incident Timeline",
    "Trace Dashboard",
    "에이전트 대화 이력",
    "툴 실행 로그",
    "Prompt/Model Operations",
    "Prompt/Model Change Approval",
    "Business Metric Gate",
    "Data Quality Drift",
    "Tool/Data Catalog",
)

LOGS_READINESS_MARKERS = (
    "app_server job → agent_server queue → app_server callback",
    "health-agent-business-metric-gate-v1",
    "health-agent-data-quality-drift-v1",
    "health-agent-incident-timeline-v1",
    "health-agent-online-quality-v1",
    "health-agent-tool-catalog-v2",
    "health-agent-trace-retention-v1",
    "Detect → Diagnose → Contain → Fix",
    "승인 요청 검증/기록",
    "freshness ok",
    "HITL required",
)

LOGS_ACTION_MARKERS = (
    "모델 설정 롤백",
    "Containment 해제",
    "영양 async 제출",
    "복약 async 제출",
    "릴리즈 리포트 생성",
    "온라인 eval 스캔 기록",
    "Testbed fallback 기록",
    "Rule-based fallback 기록",
    "Retention dry-run 기록",
    "Retention cleanup 실행",
)


def test_dashboard_uses_home_chat_tabs():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "복약 알림 + AI 에이전트 시뮬레이터" in response.text
    assert '<link rel="icon" href="data:,' in response.text
    assert "현재 에이전트 시스템 역할" in response.text
    assert "HOME" in response.text
    assert "Chat" in response.text
    assert "LOGS" in response.text
    assert "알림 센터" in response.text
    assert "에이전트와의 대화" in response.text
    assert "대화 이력" in response.text
    assert "data-chat-scroll-region" in response.text
    assert "data-chat-message-count" in response.text
    assert "data-chat-last-message-id" in response.text
    assert "에이전트와 대화" in response.text
    assert "아침 / 점심 / 야간" in response.text
    assert "혈압약" in response.text
    assert "1정" in response.text
    assert "직접 입력" in response.text
    assert "??" not in response.text
    assert "활성 알림 정책" in response.text
    assert 'id="overview-summary"' not in response.text
    assert 'class="overview-card' not in response.text
    assert ">오늘 복약</span>" not in response.text
    assert ">오늘 영양</span>" not in response.text
    assert ">활성 알림</span>" not in response.text
    assert response.text.index('class="dose-calendar"') < response.text.index('id="nutrition-panel"')
    assert response.text.index('id="active-policies-panel"') < response.text.index('id="notifications-panel"')
    assert "회 추가 /" in response.text
    assert "분 간격" in response.text
    assert "미복용 AI 알림: 예정 후 90분" in response.text
    assert "방금 질문에 답변하기" not in response.text
    assert "AI에게 말걸기" not in response.text
    assert "/chat/" in response.text
    assert "특정 시각 점프" not in response.text
    assert "/clock/jump" not in response.text
    assert 'aria-label="주요 화면"' in response.text
    assert "/?page=alerts" not in response.text
    assert "/?page=assistant" not in response.text


def test_removed_obsolete_partial_routes_return_404():
    client = TestClient(app)

    for path in (
        "/partials/clock",
        "/partials/clock-live",
        "/partials/medications",
        "/partials/overview-summary",
        "/redirect",
    ):
        response = client.get(path)
        assert response.status_code == 404


def test_removed_clock_jump_route_returns_404():
    client = TestClient(app)

    response = client.post("/clock/jump", data={"target_time": "2026-04-20T09:00"})

    assert response.status_code == 404


def test_active_dashboard_partials_still_render():
    client = TestClient(app)

    for path in (
        "/partials/time-bar",
        "/partials/timeline",
        "/partials/nutrition",
        "/partials/logs",
        "/partials/active-policies",
        "/partials/notifications",
        "/partials/chat",
        "/partials/chat-log",
        "/partials/chat-history",
        "/partials/system-request-history",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"].startswith("no-store")

    static_response = client.get("/static/notifications.js")
    assert static_response.status_code == 200
    assert static_response.headers["cache-control"].startswith("no-store")
    assert "event.altKey" in static_response.text
    assert "selectionStart" in static_response.text


def test_timeline_status_text_uses_status_color_classes():
    client = TestClient(app)

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        session.query(DoseEvent).delete()
        session.add_all(
            [
                DoseEvent(
                    patient_id="demo-patient",
                    plan_id=1,
                    schedule_id=1,
                    medication_name="색상 테스트약",
                    slot_label="아침 08:00",
                    scheduled_for=datetime(2026, 4, 20, 8, 0),
                    status="scheduled",
                ),
                DoseEvent(
                    patient_id="demo-patient",
                    plan_id=1,
                    schedule_id=2,
                    medication_name="색상 테스트약",
                    slot_label="점심 13:00",
                    scheduled_for=datetime(2026, 4, 20, 13, 0),
                    status="missed",
                ),
                DoseEvent(
                    patient_id="demo-patient",
                    plan_id=1,
                    schedule_id=3,
                    medication_name="색상 테스트약",
                    slot_label="야간 21:00",
                    scheduled_for=datetime(2026, 4, 20, 21, 0),
                    taken_at=datetime(2026, 4, 20, 22, 0),
                    status="taken",
                    note="late_taken_after_miss",
                ),
            ]
        )
        session.commit()

    response = client.get("/partials/timeline")

    assert response.status_code == 200
    assert 'class="dose-status dose-status-scheduled">scheduled</span>' in response.text
    assert 'class="dose-status dose-status-missed">missed</span>' in response.text
    assert 'class="dose-status dose-status-taken-after">taken after</span>' in response.text
    assert "late_taken_after_miss" not in response.text


def test_timeline_calendar_selects_date_and_shows_day_records():
    client = TestClient(app)

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        session.query(DoseEvent).delete()
        session.add_all(
            [
                DoseEvent(
                    patient_id="demo-patient",
                    plan_id=1,
                    schedule_id=1,
                    medication_name="오늘약",
                    slot_label="아침 08:00",
                    scheduled_for=datetime(2026, 4, 20, 8, 0),
                    status="taken",
                ),
                DoseEvent(
                    patient_id="demo-patient",
                    plan_id=1,
                    schedule_id=2,
                    medication_name="내일약",
                    slot_label="점심 13:00",
                    scheduled_for=datetime(2026, 4, 21, 13, 0),
                    status="missed",
                ),
            ]
        )
        session.commit()

    response = client.get("/partials/timeline?timeline_date=2026-04-21")

    assert response.status_code == 200
    assert "오늘 2026-04-20" in response.text
    assert "선택 2026-04-21" in response.text
    assert "내일약 / 점심 13:00" in response.text
    assert "오늘약 / 아침 08:00" not in response.text
    assert 'hx-get="/partials/timeline?timeline_date=2026-04-21"' in response.text
    assert "dose-calendar-grid" in response.text
    assert "is-selected" in response.text
    assert "has-missed" in response.text


def test_chat_log_partial_uses_js_controlled_refresh_to_preserve_scroll():
    client = TestClient(app)

    response = client.get("/partials/chat-log")

    assert response.status_code == 200
    assert "data-chat-scroll-region" in response.text
    assert "live-header" not in response.text
    assert 'hx-trigger="load, every 3s"' not in response.text
    assert 'hx-trigger="load, every 2s"' not in response.text


def test_chat_log_keeps_missed_dose_messages_in_chat_flow():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        ensure_base_data(session)
        add_chat_message(
            session,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            content="속이 메스꺼운데 약 때문일까요?",
        )
        add_chat_message(
            session,
            role="assistant",
            sender_type="assistant",
            category="missed_dose",
            content="점심 약을 놓친 이유를 알려주세요.",
        )
        session.commit()

    try:
        chat_response = client.get("/partials/chat-log")
        history_response = client.get("/partials/chat-history")

        assert chat_response.status_code == 200
        assert history_response.status_code == 200
        assert "속이 메스꺼운데 약 때문일까요?" in chat_response.text
        assert "점심 약을 놓친 이유를 알려주세요." in chat_response.text
        assert "점심 약을 놓친 이유를 알려주세요." not in history_response.text
        assert "속이 메스꺼운데 약 때문일까요?" not in history_response.text
    finally:
        with SessionLocal() as session:
            session.query(ChatMessage).delete()
            session.commit()


def test_chat_log_hides_legacy_policy_confirmation_json_reply():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        ensure_base_data(session)
        add_chat_message(
            session,
            role="user",
            sender_type="patient",
            category="reply",
            content='{"action": "increase", "label": "늘리기", "number": 1, "prompt_type": "policy_confirmation"}',
        )
        add_chat_message(
            session,
            role="assistant",
            sender_type="assistant",
            category="policy_confirmation",
            content="아침 08:00: 정책이 적용되었습니다.",
        )
        session.commit()

    try:
        chat_response = client.get("/partials/chat-log")
        history_response = client.get("/partials/chat-history")

        assert chat_response.status_code == 200
        assert history_response.status_code == 200
        assert "아침 08:00: 정책이 적용되었습니다." in chat_response.text
        assert "prompt_type" not in chat_response.text
        assert "prompt_type" not in history_response.text
        assert '{"action": "increase"' not in chat_response.text
        assert '{"action": "increase"' not in history_response.text
    finally:
        with SessionLocal() as session:
            session.query(ChatMessage).delete()
            session.commit()


def test_chat_history_records_visible_notifications():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        create_notification(
            session,
            notification_type="medication_alert",
            title="복약 알림",
            body="아침 약 복용 시간입니다.",
            visible_at=datetime(2026, 4, 20, 8, 0),
            metadata={},
        )
        create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="알림 정책 변경을 확인해주세요.",
            visible_at=datetime(2026, 4, 20, 11, 0),
            metadata={"category": "policy_confirmation", "status": "agent_ready"},
        )
        create_notification(
            session,
            notification_type="agent_error",
            title="AI 에이전트 오류",
            body="백그라운드 AI 작업을 처리하지 못했습니다.",
            visible_at=datetime(2026, 4, 20, 11, 30),
            metadata={},
        )
        session.commit()

    try:
        response = client.get("/partials/chat-history")

        assert response.status_code == 200
        assert "복약 알림: 아침 약 복용 시간입니다." in response.text
        assert "AI가 대화를 요청합니다.: 알림 정책 변경을 확인해주세요." in response.text
        assert "AI 에이전트 오류: 백그라운드 AI 작업을 처리하지 못했습니다." in response.text
        assert "복약 알림" in response.text
        assert "대화 요청" in response.text
        assert "AI 오류" in response.text
    finally:
        with SessionLocal() as session:
            session.query(Notification).delete()
            session.query(ChatMessage).delete()
            session.commit()


def test_chat_log_shows_ready_missed_dose_alert_even_if_chat_row_is_missing():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="아침 08:00 당뇨약을 놓친 이유를 알려주세요.",
            visible_at=clock.current_time,
            related_dose_event_id=None,
            metadata={"category": "missed_dose", "status": "agent_ready"},
        )
        session.commit()

    try:
        response = client.get("/partials/chat")

        assert response.status_code == 200
        assert "아침 08:00 당뇨약을 놓친 이유를 알려주세요." in response.text
        assert "AI" in response.text
        assert "미복용 대화" in response.text
        assert 'action="/chat/system"' in response.text
        assert 'name="notification_id"' not in response.text
    finally:
        with SessionLocal() as session:
            session.query(ChatMessage).delete()
            session.query(Notification).delete()
            session.commit()


def test_chat_panel_shows_ready_policy_confirmation_without_page_reload():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        session.query(Notification).delete()
        ensure_base_data(session)
        clock = ensure_clock(session)
        create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="아침 알림을 추가 2회, 10분 간격으로 변경하시는 건 어떨까요?",
            visible_at=clock.current_time,
            related_dose_event_id=None,
            metadata={
                "category": "policy_confirmation",
                "status": "agent_ready",
                "policy_change": {
                    "question": "아침 알림을 추가 2회, 10분 간격으로 변경하시는 건 어떨까요?",
                    "candidates": [
                        {
                            "slot_label": "아침 08:00",
                            "current": {"summary": "추가 1회 · 30분 간격"},
                            "proposed": {"summary": "추가 2회 · 10분 간격"},
                        }
                    ],
                },
                "multiple_choice": {
                    "prompt_type": "policy_confirmation",
                    "question": "정책 변경 방향을 선택해주세요.",
                    "options": [
                        {"number": 1, "value": "increase", "label": "늘리기", "text": "1. 늘리기", "description": ""},
                        {"number": 2, "value": "keep", "label": "현행 유지하기", "text": "2. 현행 유지하기", "description": ""},
                    ],
                },
            },
        )
        session.commit()

    try:
        response = client.get("/partials/chat")

        assert response.status_code == 200
        assert "아침 알림을 추가 2회, 10분 간격으로 변경하시는 건 어떨까요?" in response.text
        assert "policy-change-table" in response.text
        assert "아침 08:00" in response.text
        assert "추가 2회 · 10분 간격" in response.text
        assert "늘리기" in response.text
        assert "현행 유지하기" in response.text
        assert 'action="/chat/policy-confirmation"' in response.text
        assert 'name="notification_id"' in response.text
    finally:
        with SessionLocal() as session:
            session.query(ChatMessage).delete()
            session.query(Notification).delete()
            session.commit()


def test_chat_panel_renders_diet_recommendation_cards():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        ensure_base_data(session)
        add_chat_message(
            session,
            role="assistant",
            content="신장 건강을 위한 추천 후보를 아래에 준비했어요.",
            sender_type="assistant",
            category="multiturn_chat",
            metadata={
                "diet_recommendations": {
                    "recommendations": [
                        {
                            "food_ref_id": "tofu-salad",
                            "food_name": "두부 샐러드",
                            "category": "샐러드",
                            "serving_size": 180,
                            "nutrients": {
                                "energy": {"value": 210, "unit": "kcal"},
                                "protein": {"value": 12, "unit": "g"},
                                "sodium": {"value": 180, "unit": "mg"},
                                "fat": {"value": 7, "unit": "g"},
                                "carbohydrate": {"value": 18, "unit": "g"},
                            },
                            "recommendation_reasons": ["나트륨 180mg으로 목표(500mg 이하)에 맞아요."],
                        }
                    ],
                    "constraints_applied": {"나트륨": "low"},
                    "blocked_count": 1,
                    "total_candidates": 8,
                }
            },
        )
        session.commit()

    try:
        response = client.get("/partials/chat")

        assert response.status_code == 200
        assert "신장 건강을 위한 추천 후보를 아래에 준비했어요." in response.text
        assert "diet-recommendation-card" in response.text
        assert "두부 샐러드" in response.text
        assert "210kcal / 180g" in response.text
        assert "단백질 12g" in response.text
        assert "나트륨 180mg" in response.text
        assert "목표(500mg 이하)에 맞아요" in response.text
    finally:
        with SessionLocal() as session:
            session.query(ChatMessage).delete()
            session.commit()


def test_chat_log_partial_keeps_latest_messages_after_history_limit():
    client = TestClient(app)

    with SessionLocal() as session:
        session.query(ChatMessage).delete()
        ensure_base_data(session)
        base_time = datetime(2026, 5, 13, 9, 0, 0)
        for index in range(55):
            message = add_chat_message(
                session,
                role="user",
                sender_type="patient",
                category="multiturn_chat",
                content=f"오래된 대화 {index}",
            )
            message.created_at = base_time + timedelta(minutes=index)
        latest_message = add_chat_message(
            session,
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            content="최신 에이전트 답변입니다.",
        )
        latest_message.created_at = base_time + timedelta(minutes=60)
        session.commit()

    try:
        response = client.get("/partials/chat-log")

        assert response.status_code == 200
        assert "최신 에이전트 답변입니다." in response.text
        assert "오래된 대화 0" not in response.text
    finally:
        with SessionLocal() as session:
            session.query(ChatMessage).delete()
            session.commit()


def test_sorted_chat_views_keeps_numeric_message_order_for_same_timestamp():
    same_time = datetime(2026, 5, 13, 9, 0, 0)
    views = [
        {"id": 8, "sort_at": same_time, "sort_order": 0, "sort_source": 0, "sort_sequence": 8},
        {"id": 9, "sort_at": same_time, "sort_order": 0, "sort_source": 0, "sort_sequence": 9},
        {"id": 10, "sort_at": same_time, "sort_order": 0, "sort_source": 0, "sort_sequence": 10},
        {"id": 11, "sort_at": same_time, "sort_order": 0, "sort_source": 0, "sort_sequence": 11},
        {"id": 12, "sort_at": same_time, "sort_order": 0, "sort_source": 0, "sort_sequence": 12},
    ]

    sorted_views = sorted_chat_views(views)

    assert [row["id"] for row in sorted_views] == [8, 9, 10, 11, 12]
    assert all("sort_at" not in row for row in sorted_views)
    assert all("sort_sequence" not in row for row in sorted_views)


def test_chat_log_partial_shows_pending_agent_response_indicator():
    client = TestClient(app)
    created_message_id = None
    created_notification_id = None

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        message = add_chat_message(
            session,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            content="속이 메스꺼운데 약 때문일까요?",
        )
        message.created_at = datetime(2026, 5, 12, 2, 16, 0)
        notification = create_notification(
            session,
            notification_type="system_policy_request",
            title="에이전트 대화 전송",
            body="에이전트에게 메시지를 보냈습니다.",
            visible_at=clock.current_time,
            metadata={
                "category": "agent_conversation",
                "status": "sent",
                "event_type": "multiturn_chat",
                "request_message": message.content,
            },
        )
        notification.created_at = utc_now()
        created_message_id = message.id
        created_notification_id = notification.id
        session.commit()

    try:
        response = client.get("/partials/chat-log")

        assert response.status_code == 200
        assert "속이 메스꺼운데 약 때문일까요?" in response.text
        assert "응답 생성 중" in response.text
        assert response.text.index("속이 메스꺼운데 약 때문일까요?") < response.text.index("응답 생성 중")
        assert "progress-round" in response.text
        assert "pending-response" in response.text
        assert 'hx-get="/partials/chat-log"' in response.text
        assert 'hx-trigger="every 2s"' in response.text
    finally:
        with SessionLocal() as session:
            if created_notification_id is not None:
                session.query(Notification).filter(Notification.id == created_notification_id).delete()
            if created_message_id is not None:
                session.query(ChatMessage).filter(ChatMessage.id == created_message_id).delete()
            session.commit()


def test_chat_log_partial_keeps_pending_indicator_during_async_continuation():
    client = TestClient(app)
    created_message_ids = []
    created_notification_id = None

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        user_message = add_chat_message(
            session,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            content="속이 메스꺼운데 약 때문일까요?",
        )
        ack_message = add_chat_message(
            session,
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            content="증상 내용을 확인해서 문항을 준비할게요.",
        )
        notification = create_notification(
            session,
            notification_type="system_policy_request",
            title="에이전트 대화 전송",
            body="에이전트에게 메시지를 보냈습니다.",
            visible_at=clock.current_time,
            metadata={
                "category": "agent_conversation",
                "status": "answered",
                "event_type": "multiturn_chat",
                "request_message": user_message.content,
                "result_message": ack_message.content,
                "async_continuation_status": "pending",
                "async_continuation_type": "side_effect_assessment",
            },
        )
        notification.created_at = utc_now()
        created_message_ids = [user_message.id, ack_message.id]
        created_notification_id = notification.id
        session.commit()

    try:
        response = client.get("/partials/chat-log")

        assert response.status_code == 200
        assert "속이 메스꺼운데 약 때문일까요?" in response.text
        assert "증상 내용을 확인해서 문항을 준비할게요." in response.text
        assert "응답 생성 중" in response.text
        assert "progress-round" in response.text
        assert "pending-response" in response.text
        assert 'hx-get="/partials/chat-log"' in response.text
    finally:
        with SessionLocal() as session:
            if created_notification_id is not None:
                session.query(Notification).filter(Notification.id == created_notification_id).delete()
            if created_message_ids:
                session.query(ChatMessage).filter(ChatMessage.id.in_(created_message_ids)).delete()
            session.commit()


def test_chat_log_partial_hides_stale_pending_agent_response_indicator():
    client = TestClient(app)
    created_message_id = None
    created_notification_id = None

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        message = add_chat_message(
            session,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            content="이전 요청입니다.",
        )
        notification = create_notification(
            session,
            notification_type="system_policy_request",
            title="에이전트 대화 전송",
            body="에이전트에게 메시지를 보냈습니다.",
            visible_at=clock.current_time,
            metadata={
                "category": "agent_conversation",
                "status": "sent",
                "event_type": "multiturn_chat",
                "request_message": message.content,
            },
        )
        notification.created_at = datetime(2000, 1, 1, 0, 0, 0)
        created_message_id = message.id
        created_notification_id = notification.id
        session.commit()

    try:
        response = client.get("/partials/chat-log")

        assert response.status_code == 200
        assert "이전 요청입니다." in response.text
        assert "응답 생성 중" not in response.text
    finally:
        with SessionLocal() as session:
            if created_notification_id is not None:
                session.query(Notification).filter(Notification.id == created_notification_id).delete()
            if created_message_id is not None:
                session.query(ChatMessage).filter(ChatMessage.id == created_message_id).delete()
            session.commit()


def test_chat_message_view_labels_patient_as_user():
    message = SimpleNamespace(
        id=1,
        role="user",
        sender_type="patient",
        category="chat",
        content="속이 메스꺼워요.",
        created_at=datetime(2026, 5, 11, 11, 17),
    )

    view = chat_message_view(message)

    assert view["role_label"] == "User"


def test_add_chat_message_uses_simulation_clock_timestamp():
    created_message_id = None
    test_time = datetime(2026, 5, 12, 14, 35, 0)

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = test_time
        message = add_chat_message(
            session,
            role="assistant",
            sender_type="assistant",
            category="missed_dose",
            content="복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
        )
        created_message_id = message.id
        view = chat_message_view(message)

        assert message.created_at == test_time
        assert view["time_label"] == "05-12 14:35"
        session.commit()

    with SessionLocal() as session:
        if created_message_id is not None:
            session.query(ChatMessage).filter(ChatMessage.id == created_message_id).delete()
            session.commit()


def test_htmx_lite_rebinds_polling_after_outer_html_swap():
    script = open("system_app/static/htmx-lite.js", encoding="utf-8").read()

    assert "bindTriggers(document, { runLoad: false })" in script
    assert "if (!elt.isConnected)" in script
    assert "window.clearInterval(everyTimer)" in script
    assert 'document.body.classList.add("htmx-request")' in script
    assert "target.replaceWith(replacement)" in script
    assert 'dispatch("htmx:afterSwap", swappedTarget || target || source' in script


def test_htmx_lite_does_not_treat_polling_containers_as_click_triggers():
    script = open("system_app/static/htmx-lite.js", encoding="utf-8").read()

    assert 'closest("[hx-get], button[hx-post]")' not in script
    assert "function clickTriggerFor" in script
    assert 'button[hx-get]' in script
    assert "submitterAction" in script


def test_styles_make_top_time_value_larger_and_lock_submitted_replies():
    styles = open("system_app/static/styles.css", encoding="utf-8").read()
    agent_styles = open("system_app/static/agent_interactions.css", encoding="utf-8").read()
    script = open("system_app/static/notifications.js", encoding="utf-8").read()
    policy_script = open("system_app/static/notifications/policy_confirmation.js", encoding="utf-8").read()

    assert ".top-time-value" in styles
    assert "font-size: 2.1rem" in styles
    assert ".notification-reply-input.is-submitted" in styles
    assert ".alert-agent-reply.is-success" in styles
    assert ".notification.conversation_alert.reply-success" in styles
    assert ".popup-toast.conversation_alert.reply-success" in styles
    assert ".alert-agent-progress" in styles
    assert ".chat-message.pending-response" in agent_styles
    assert "max-height: min(520px, 56vh);" in agent_styles
    assert ".agent-chat-window" in agent_styles
    assert "border-radius: 16px;" in agent_styles
    assert "linear-gradient(180deg" in agent_styles
    assert ".policy-change-table" in agent_styles
    assert ".policy-choice-button" in agent_styles
    assert ".conversation-history-log .chat-message.from-system" in agent_styles
    assert "align-self: flex-start;" in agent_styles
    assert "background: rgba(255, 255, 255, 0.96);" in agent_styles
    chat_scroll_script = open("system_app/static/notifications/chat_scroll.js", encoding="utf-8").read()
    assert "chatScrollForceBottom" in chat_scroll_script
    assert "getChatComposerFromRequestEvent" in chat_scroll_script
    assert "getChatLogSignature" in chat_scroll_script
    assert "chatLogChanged" in chat_scroll_script
    assert "restoreChatLogScrollAfterLayout" in chat_scroll_script
    assert "setTimeout" in chat_scroll_script
    assert "form.reset()" in chat_scroll_script
    assert "lockSubmittedReplyForm" in script
    assert "showAgentProcessing" in script
    assert "installChatComposerPendingIndicator()" in script
    assert "data-local-pending-agent" in script
    assert "htmx:afterSwap" in script
    assert "chatLogNeedsPending" in script
    assert "localChatPendingActive" in script
    assert "syncLocalPending" in script
    assert "createPolicyConfirmationRenderer" in script
    assert "after_id=${state.lastSeenId}&_=${Date.now()}" in script
    assert "policyChoicePayload" in policy_script
    assert "createPolicyChangeTable" in policy_script
    assert "import { createPanelRefresher }" in script
    assert "installChatLogScrollPreserver(state)" in script
    assert "installAppTabs()" in script
    assert "openChatPage()" in script
    assert "#logs" in script
    assert "logs-page" in script
    assert "data-scroll-target" in script
    assert "scrollIntoView" in script
    assert "preventScroll: true" in script
    assert "state.chatScrollForceBottom = true" in script
    assert "hasNewChatPromptNotification" in script
    assert "refreshChatPanel();" in script
    assert "await Promise.all([refreshChatPanel(), refreshChatHistoryPanel()])" in script
    assert ".chat-page-shell" in styles
    assert "max-width: none" in styles
    assert ".chat-action-card" in agent_styles
    assert "overflow-anchor: none" in agent_styles
    panel_refresher = open("system_app/static/notifications/panels.js", encoding="utf-8").read()
    assert "data-agent-processing" in panel_refresher
    assert "cacheBustedUrl" in panel_refresher
    assert "dispatchLifecycleEvent" in panel_refresher
    assert "htmx:beforeSwap" in panel_refresher
    assert "htmx:afterSwap" in panel_refresher
    assert 'replacePanel("/partials/chat", "#chat-panel")' in panel_refresher
    assert 'replacePanel("/partials/chat-log", "#chat-log-region")' in panel_refresher
    assert "return Promise.all" in panel_refresher
    assert 'fetch(url, { headers: { "HX-Request": "true" } })' in panel_refresher
    assert "function refreshChangedNotifications()" in panel_refresher
    assert "refreshActiveConversationPopups();" in panel_refresher


def test_logs_partial_renders_observability_sections():
    client = TestClient(app)

    response = client.get("/partials/logs")

    assert response.status_code == 200
    assert 'id="logs-panel"' in response.text
    assert "logs-hero-panel" not in response.text
    assert "<h2>Observability</h2>" not in response.text
    for marker in LOGS_SECTION_MARKERS:
        assert marker in response.text
    for marker in LOGS_READINESS_MARKERS:
        assert marker in response.text
    for marker in LOGS_ACTION_MARKERS:
        assert marker in response.text
    assert "Replay 상세" in response.text or "Trace를 선택하면 replay/detail payload" in response.text
    assert "Replay artifact 생성" in response.text or "Trace를 선택하면 replay/detail payload" in response.text


def test_logs_partial_renders_trace_drilldown_and_eval_action():
    client = TestClient(app)
    trace_id = "test-logs-trace-drilldown"

    with SessionLocal() as session:
        session.query(AgentRunStep).filter(AgentRunStep.trace_id == trace_id).delete()
        session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
        session.add(
            AgentRunTrace(
                trace_id=trace_id,
                workflow_name="daily_recommendation_async",
                source_event_type="daily_recommendation_async",
                status="failed",
                agent_name="daily_recommendation_agent",
                decision_type="daily_recommendation",
                provider="bedrock_anthropic",
                model_id="test-model",
                latency_ms=3210,
                tool_count=1,
                error_message="Bedrock provider timeout",
            )
        )
        session.add(
            AgentRunStep(
                trace_id=trace_id,
                step_type="tool_call",
                step_name="get_nutrition_status",
                status="success",
                latency_ms=42,
                tool_name="get_nutrition_status",
                side_effect_level="read_only",
            )
        )
        session.commit()

    try:
        response = client.get("/partials/logs")

        assert response.status_code == 200
        assert "Root-cause detail" in response.text
        assert "Eval backlog 등록" in response.text
        assert "Replay 상세" in response.text
        assert "Replay artifact 생성" in response.text
        assert 'name="trace_id" value="test-logs-trace-drilldown"' in response.text
        assert "get_nutrition_status" in response.text

        detail_response = client.get(f"/partials/logs/traces/{trace_id}")

        assert detail_response.status_code == 200
        assert 'id="trace-replay-panel"' in detail_response.text
        assert "Replay plan" in detail_response.text
        assert "daily_recommendation_async" in detail_response.text
        assert "get_nutrition_status" in detail_response.text
        assert "Replay artifact + eval backlog 등록" in detail_response.text
    finally:
        with SessionLocal() as session:
            session.query(AgentRunStep).filter(AgentRunStep.trace_id == trace_id).delete()
            session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
            session.commit()


def test_logs_tool_execution_summary_redacts_audit_human_summary():
    client = TestClient(app)
    trace_id = "test-logs-audit-summary-redaction"
    raw_summary = "pytest-redaction-peanut-7755 allergy and pytest-redaction-noodle-7755 dislike"

    with SessionLocal() as session:
        session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == trace_id).delete()
        session.add(
            AgentDecisionAudit(
                trace_id=trace_id,
                agent_name="nutrition_agent",
                prompt_version_id="test",
                decision_type="multiturn_chat",
                structured_payload="{}",
                human_summary=raw_summary,
                applied=False,
                error_message="",
                source_event_type="multiturn_chat",
            )
        )
        session.commit()

    try:
        response = client.get("/partials/logs")

        assert response.status_code == 200
        assert raw_summary not in response.text
        assert "clinical text redacted" in response.text
        assert "pytest-redaction-peanut-7755" not in response.text
        assert "pytest-redaction-noodle-7755" not in response.text
    finally:
        with SessionLocal() as session:
            session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == trace_id).delete()
            session.commit()


def test_promote_observability_eval_case_writes_backlog(tmp_path, monkeypatch):
    from system_app.services import observability_actions

    trace_id = "test-eval-backlog-trace"
    backlog_path = tmp_path / "agent_eval_backlog.json"
    monkeypatch.setattr(observability_actions, "EVAL_BACKLOG_PATH", backlog_path)

    with SessionLocal() as session:
        session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
        session.add(
            AgentRunTrace(
                trace_id=trace_id,
                workflow_name="nutrition_daily_recommendation",
                source_event_type="daily_recommendation_async",
                status="failed",
                agent_name="daily_recommendation_agent",
                decision_type="daily_recommendation",
                provider="bedrock_anthropic",
                model_id="test-model",
                error_message="Bedrock provider timeout",
            )
        )
        session.commit()

        result = observability_actions.promote_observability_eval_case(
            session,
            source_type="trace",
            trace_id=trace_id,
            reason="pytest promotion",
        )
        duplicate = observability_actions.promote_observability_eval_case(
            session,
            source_type="trace",
            trace_id=trace_id,
            reason="pytest promotion",
        )

        session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
        session.commit()

    cases = json.loads(backlog_path.read_text(encoding="utf-8"))
    assert result["success"] is True
    assert result["created"] is True
    assert duplicate["created"] is False
    assert len(cases) == 1
    assert cases[0]["layer"] == "behavioral"
    assert cases[0]["owner"] == "nutrition-agent-owner"
    assert cases[0]["pass_gate"]["trace_replay_required"] is True
    assert cases[0]["lifecycle"]["status"] == "open"
    assert cases[0]["lifecycle"]["history"][0]["to"] == "open"


def test_logs_partial_shows_eval_backlog_suite_status(tmp_path, monkeypatch):
    from system_app.services import observability_view

    client = TestClient(app)
    backlog_path = tmp_path / "agent_eval_backlog.json"
    backlog_path.write_text(
        json.dumps(
            [
                _eval_backlog_case("prod-trace-ui-included", severity="low", layer="deterministic"),
                _eval_backlog_case("prod-trace-ui-blocking", severity="high", layer="behavioral"),
                _eval_backlog_case("PROD-EVAL-001", severity="low", layer="deterministic"),
                {"id": "prod-trace-ui-invalid"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(observability_view, "EVAL_BACKLOG_PATH", backlog_path)

    response = client.get("/partials/logs")

    assert response.status_code == 200
    assert "included 2" in response.text
    assert "excluded 2" in response.text
    assert "CI blocking 1" in response.text
    assert "open 4" in response.text
    assert "reviewed 0" in response.text
    assert "cleared 0" in response.text
    assert "eval suite included" in response.text
    assert "CI blocking" in response.text
    assert "release_blocked" in response.text
    assert "hard blocker · Eval backlog CI gate" in response.text
    assert "lifecycle open" in response.text
    assert "duplicate excluded" in response.text
    assert "schema invalid" in response.text
    assert "reviewed 표시" in response.text


def test_logs_eval_backlog_lifecycle_action_clears_ci_gate(tmp_path, monkeypatch):
    from system_app.services import observability_actions, observability_view

    client = TestClient(app)
    backlog_path = tmp_path / "agent_eval_backlog.json"
    backlog_path.write_text(
        json.dumps([_eval_backlog_case("prod-trace-ui-clearable", severity="high", layer="behavioral")]),
        encoding="utf-8",
    )
    monkeypatch.setattr(observability_actions, "EVAL_BACKLOG_PATH", backlog_path)
    monkeypatch.setattr(observability_view, "EVAL_BACKLOG_PATH", backlog_path)

    response = client.post(
        "/logs/eval-backlog/status",
        data={
            "case_id": "prod-trace-ui-clearable",
            "status": "cleared",
            "reason": "pytest regression evidence attached",
        },
    )

    cases = json.loads(backlog_path.read_text(encoding="utf-8"))
    assert response.status_code == 200
    assert cases[0]["lifecycle"]["status"] == "cleared"
    assert cases[0]["lifecycle"]["history"][-1]["to"] == "cleared"
    assert "CI blocking 0" in response.text
    assert "cleared 1" in response.text
    assert "cleared from CI gate" in response.text


def test_logs_governance_change_log_action_records_entry(tmp_path, monkeypatch):
    from system_app.services import governance_change_log

    client = TestClient(app)
    change_log_path = tmp_path / "agent_change_log.json"
    monkeypatch.setattr(governance_change_log, "CHANGE_LOG_PATH", change_log_path)

    response = client.post(
        "/logs/governance/change-log",
        data={
            "change_type": "prompt",
            "target": "PROMPT_VERSION_ID:test",
            "summary": "tightened high-risk tool instruction",
            "owner": "prompt-owner",
            "rollback": "restore previous prompt version",
            "evidence": "pytest change log route",
        },
    )

    entries = json.loads(change_log_path.read_text(encoding="utf-8"))
    assert response.status_code == 200
    assert len(entries) == 1
    assert entries[0]["change_type"] == "prompt"
    assert entries[0]["target"] == "PROMPT_VERSION_ID:test"
    assert "tightened high-risk tool instruction" in response.text


def test_logs_online_eval_scan_action_records_artifact(tmp_path, monkeypatch):
    from system_app.services import readiness_controls

    client = TestClient(app)
    trace_id = "test-online-eval-scan"
    findings_path = tmp_path / "agent_online_eval_findings.json"
    monkeypatch.setattr(readiness_controls, "ONLINE_EVAL_FINDINGS_PATH", findings_path)

    with SessionLocal() as session:
        session.query(AgentRunStep).filter(AgentRunStep.trace_id == trace_id).delete()
        session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
        session.add(
            AgentRunTrace(
                trace_id=trace_id,
                workflow_name="multiturn_chat",
                source_event_type="multiturn_chat",
                status="failed",
                agent_name="system_event_agent",
                decision_type="system_guidance",
                error_message="provider timeout",
            )
        )
        session.commit()

    try:
        response = client.post("/logs/online-eval/scan")

        scans = json.loads(findings_path.read_text(encoding="utf-8"))
        assert response.status_code == 200
        assert len(scans) == 1
        assert scans[0]["summary"]["status"] == "fail"
        assert any(finding["trace_id"] == trace_id for finding in scans[0]["findings"])
        assert "online eval scan recorded" in response.text
    finally:
        with SessionLocal() as session:
            session.query(AgentRunStep).filter(AgentRunStep.trace_id == trace_id).delete()
            session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
            session.commit()


def test_logs_trace_replay_artifact_action_writes_json(tmp_path, monkeypatch):
    from system_app.services import trace_replay

    client = TestClient(app)
    trace_id = "test-trace-replay-artifact"
    monkeypatch.setattr(trace_replay, "TRACE_REPLAY_OUTPUT_DIR", tmp_path)

    with SessionLocal() as session:
        session.query(AgentRunStep).filter(AgentRunStep.trace_id == trace_id).delete()
        session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
        session.add(
            AgentRunTrace(
                trace_id=trace_id,
                workflow_name="daily_recommendation_async",
                source_event_type="daily_recommendation_async",
                status="failed",
                agent_name="daily_recommendation_agent",
                decision_type="daily_recommendation",
                model_id="test-model",
                error_message="provider timeout",
            )
        )
        session.commit()

    try:
        response = client.post("/logs/traces/replay-artifact", data={"trace_id": trace_id})

        output_path = tmp_path / f"{trace_id}.json"
        artifact = json.loads(output_path.read_text(encoding="utf-8"))
        assert response.status_code == 200
        assert artifact["artifact_type"] == "trace_replay"
        assert artifact["trace"]["trace_id"] == trace_id
        assert "trace replay artifact written" in response.text
    finally:
        with SessionLocal() as session:
            session.query(AgentRunStep).filter(AgentRunStep.trace_id == trace_id).delete()
            session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
            session.commit()


def test_logs_trace_replay_to_eval_action_writes_artifact_and_backlog(tmp_path, monkeypatch):
    from system_app.services import observability_actions, observability_view, trace_replay

    client = TestClient(app)
    trace_id = "test-trace-replay-to-eval"
    replay_dir = tmp_path / "replays"
    backlog_path = tmp_path / "agent_eval_backlog.json"
    monkeypatch.setattr(trace_replay, "TRACE_REPLAY_OUTPUT_DIR", replay_dir)
    monkeypatch.setattr(observability_actions, "EVAL_BACKLOG_PATH", backlog_path)
    monkeypatch.setattr(observability_view, "EVAL_BACKLOG_PATH", backlog_path)

    with SessionLocal() as session:
        session.query(AgentRunStep).filter(AgentRunStep.trace_id == trace_id).delete()
        session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
        session.add(
            AgentRunTrace(
                trace_id=trace_id,
                workflow_name="nutrition_daily_recommendation",
                source_event_type="daily_recommendation_async",
                status="failed",
                agent_name="daily_recommendation_agent",
                decision_type="daily_recommendation",
                model_id="test-model",
                error_message="provider timeout",
            )
        )
        session.commit()

    try:
        response = client.post("/logs/traces/replay-to-eval", data={"trace_id": trace_id})

        replay_path = replay_dir / f"{trace_id}.json"
        artifact = json.loads(replay_path.read_text(encoding="utf-8"))
        cases = json.loads(backlog_path.read_text(encoding="utf-8"))
        assert response.status_code == 200
        assert artifact["artifact_type"] == "trace_replay"
        assert len(cases) == 1
        assert cases[0]["input"]["trace_id"] == trace_id
        assert cases[0]["input"]["trace_replay_artifact"] == str(replay_path)
        assert cases[0]["source"]["artifact_path"] == str(replay_path)
        assert "trace replay artifact promoted to eval backlog" in response.text
    finally:
        with SessionLocal() as session:
            session.query(AgentRunStep).filter(AgentRunStep.trace_id == trace_id).delete()
            session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == trace_id).delete()
            session.commit()


def test_logs_release_readiness_report_action_writes_json_and_markdown(tmp_path, monkeypatch):
    from system_app.services import release_readiness

    client = TestClient(app)
    report_dir = tmp_path / "readiness"
    eval_dir = tmp_path / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "agent-eval-latest.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "generated_at": "2026-06-20T00:00:00+00:00",
                "summary": {"passed": 8, "failed": 0, "review_required": 0},
                "case_count": 30,
                "selected_count": 8,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(release_readiness, "RELEASE_READINESS_OUTPUT_DIR", report_dir)
    monkeypatch.setattr(release_readiness, "EVAL_REPORTS_DIR", eval_dir)

    response = client.post("/logs/release-readiness/report")

    json_reports = list(report_dir.glob("release-readiness-*.json"))
    markdown_reports = list(report_dir.glob("release-readiness-*.md"))
    assert response.status_code == 200
    assert len(json_reports) == 1
    assert len(markdown_reports) == 1
    report = json.loads(json_reports[0].read_text(encoding="utf-8"))
    markdown = markdown_reports[0].read_text(encoding="utf-8")
    assert report["artifact_type"] == "release_readiness_report"
    assert report["latest_eval"]["status"] == "ok"
    assert "scorecard" in report
    assert "# Release Readiness Report" in markdown
    assert "release readiness report written" in response.text


def test_logs_model_fallback_drill_action_records_artifact(tmp_path, monkeypatch):
    from system_app.services import readiness_controls

    async def fake_model_config(_settings):
        return {
            "provider": "bedrock_anthropic",
            "model_tier": "fast",
            "model_id": "test-model",
            "available_tiers": {"fast": "test-model"},
        }

    client = TestClient(app)
    drill_path = tmp_path / "model_fallback_drills.json"
    monkeypatch.setattr(readiness_controls, "MODEL_FALLBACK_DRILL_PATH", drill_path)
    monkeypatch.setattr(readiness_controls, "_fetch_agent_model_config", fake_model_config)

    response = client.post("/logs/model-fallback/drill", data={"drill_mode": "testbed"})

    drills = json.loads(drill_path.read_text(encoding="utf-8"))
    assert response.status_code == 200
    assert len(drills) == 1
    assert drills[0]["drill_mode"] == "testbed"
    assert drills[0]["fallback_provider"] == "rule_based"
    assert drills[0]["containment"]["owner"] == "ai-ops-owner"
    assert "testbed model fallback drill recorded" in response.text


def test_logs_model_config_rollback_updates_agent_and_records_change(tmp_path, monkeypatch):
    from shared.schemas import AgentModelConfig
    from system_app.services import governance_change_log
    from system_app.services.agent_client import AgentClient

    async def fake_set_model_tier(self, model_tier):
        return AgentModelConfig(
            provider="bedrock_anthropic",
            model_tier=model_tier,
            model_id="test-rollback-model",
            available_tiers={"fast": "test-fast-model", "sonnet": "test-sonnet-model"},
        )

    client = TestClient(app)
    change_log_path = tmp_path / "agent_change_log.json"
    monkeypatch.setattr(governance_change_log, "CHANGE_LOG_PATH", change_log_path)
    monkeypatch.setattr(AgentClient, "set_model_tier", fake_set_model_tier)

    response = client.post(
        "/logs/model-config/rollback",
        data={
            "model_tier": "fast",
            "reason": "pytest rollback",
        },
    )

    entries = json.loads(change_log_path.read_text(encoding="utf-8"))
    assert response.status_code == 200
    assert len(entries) == 1
    assert entries[0]["change_type"] == "model"
    assert entries[0]["target"] == "model_tier:fast"
    assert entries[0]["summary"] == "pytest rollback"
    assert "model config rolled back to fast" in response.text


def test_logs_trace_retention_cleanup_records_audit_and_redacts_old_spans(tmp_path, monkeypatch):
    from system_app.services import trace_retention

    client = TestClient(app)
    audit_path = tmp_path / "trace_retention_audit.json"
    monkeypatch.setattr(trace_retention, "TRACE_RETENTION_AUDIT_PATH", audit_path)
    old_trace_id = "test-retention-old-trace"
    recent_trace_id = "test-retention-recent-trace"
    old_at = utc_now() - timedelta(days=45)
    recent_at = utc_now()

    with SessionLocal() as session:
        session.query(AgentRunStep).filter(AgentRunStep.trace_id.in_([old_trace_id, recent_trace_id])).delete(synchronize_session=False)
        session.query(AgentRunTrace).filter(AgentRunTrace.trace_id.in_([old_trace_id, recent_trace_id])).delete(synchronize_session=False)
        session.add(
            AgentRunTrace(
                trace_id=old_trace_id,
                workflow_name="retention_cleanup",
                source_event_type="retention_cleanup",
                status="completed",
                completed_at=old_at,
                created_at=old_at,
                updated_at=old_at,
            )
        )
        session.add(
            AgentRunTrace(
                trace_id=recent_trace_id,
                workflow_name="retention_cleanup",
                source_event_type="retention_cleanup",
                status="completed",
                completed_at=recent_at,
                created_at=recent_at,
                updated_at=recent_at,
            )
        )
        session.add(AgentRunStep(trace_id=old_trace_id, step_type="tool_call", step_name="old_span", status="success"))
        session.add(AgentRunStep(trace_id=recent_trace_id, step_type="tool_call", step_name="recent_span", status="success"))
        session.commit()

    try:
        dry_run_response = client.post("/logs/traces/retention-cleanup", data={"mode": "dry_run"})

        dry_run_audit = json.loads(audit_path.read_text(encoding="utf-8"))
        assert dry_run_response.status_code == 200
        assert dry_run_audit[-1]["mode"] == "dry_run"
        assert dry_run_audit[-1]["expired_trace_count"] == 1
        assert dry_run_audit[-1]["candidate_span_count"] == 1
        assert dry_run_audit[-1]["expired_traces"][0]["trace_id"] == old_trace_id
        with SessionLocal() as session:
            assert session.query(AgentRunStep).filter(AgentRunStep.trace_id == old_trace_id).count() == 1

        execute_response = client.post("/logs/traces/retention-cleanup", data={"mode": "execute"})

        execute_audit = json.loads(audit_path.read_text(encoding="utf-8"))
        assert execute_response.status_code == 200
        assert execute_audit[-1]["mode"] == "execute"
        assert execute_audit[-1]["redacted_span_count"] == 1
        assert "trace retention cleanup executed" in execute_response.text
        with SessionLocal() as session:
            assert session.query(AgentRunTrace).filter(AgentRunTrace.trace_id == old_trace_id).count() == 1
            assert session.query(AgentRunStep).filter(AgentRunStep.trace_id == old_trace_id).count() == 0
            assert session.query(AgentRunStep).filter(AgentRunStep.trace_id == recent_trace_id).count() == 1
    finally:
        with SessionLocal() as session:
            session.query(AgentRunStep).filter(AgentRunStep.trace_id.in_([old_trace_id, recent_trace_id])).delete(synchronize_session=False)
            session.query(AgentRunTrace).filter(AgentRunTrace.trace_id.in_([old_trace_id, recent_trace_id])).delete(synchronize_session=False)
            session.commit()


def _eval_backlog_case(case_id: str, *, severity: str, layer: str) -> dict:
    return {
        "id": case_id,
        "title": f"Backlog UI case {case_id}",
        "layer": layer,
        "intent": "incident_regression",
        "risk": "async_job_failure",
        "input": {"source_type": "trace", "trace_id": case_id, "summary": "synthetic incident"},
        "expected_behavior": "The agent keeps bounded retries and exposes trace evidence.",
        "prohibited_behavior": "The agent must not retry forever or hide the failed workflow.",
        "tags": ["evaluation", "observability", "incident", "async"],
        "owner": "eval-owner",
        "severity": severity,
        "synthetic": True,
        "review_cadence": "incident-review",
        "pass_gate": {"trace_replay_required": True},
    }
