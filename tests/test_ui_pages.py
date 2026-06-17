from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from system_app.db import SessionLocal
from system_app.main import app
from system_app.models import ChatMessage, DoseEvent, Notification
from system_app.services.clock_service import ensure_clock
from system_app.services.dashboard_view import chat_message_view
from system_app.services.notification_service import create_notification
from system_app.services.patient_profile_service import ensure_base_data
from system_app.services.timeline_service import add_chat_message


def test_dashboard_uses_home_chat_tabs():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "복약 알림 + AI 에이전트 시뮬레이터" in response.text
    assert '<link rel="icon" href="data:,' in response.text
    assert "현재 에이전트 시스템 역할" in response.text
    assert "HOME" in response.text
    assert "Chat" in response.text
    assert "알림 센터" not in response.text
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
        notification.created_at = datetime.utcnow()
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
    assert "form.reset()" in chat_scroll_script
    assert "lockSubmittedReplyForm" in script
    assert "showAgentProcessing" in script
    assert "createPolicyConfirmationRenderer" in script
    assert "after_id=${state.lastSeenId}&_=${Date.now()}" in script
    assert "policyChoicePayload" in policy_script
    assert "createPolicyChangeTable" in policy_script
    assert "import { createPanelRefresher }" in script
    assert "installChatLogScrollPreserver(state)" in script
    assert "installAppTabs()" in script
    assert "openChatPage()" in script
    assert "state.chatScrollForceBottom = true" in script
    assert "hasNewChatPromptNotification" in script
    assert "refreshChatPanel();" in script
    assert "await Promise.all([refreshChatPanel(), refreshChatHistoryPanel()])" in script
    assert ".chat-page-shell" in styles
    assert "max-width: none" in styles
    assert ".chat-action-card" in agent_styles
    panel_refresher = open("system_app/static/notifications/panels.js", encoding="utf-8").read()
    assert "data-agent-processing" in panel_refresher
    assert "cacheBustedUrl" in panel_refresher
    assert 'replacePanel("/partials/chat", "#chat-panel")' in panel_refresher
    assert 'replacePanel("/partials/chat-log", "#chat-log-region")' in panel_refresher
    assert "return Promise.all" in panel_refresher
    assert 'fetch(url, { headers: { "HX-Request": "true" } })' in panel_refresher
    assert "function refreshChangedNotifications()" in panel_refresher
    assert "refreshActiveConversationPopups();" in panel_refresher
