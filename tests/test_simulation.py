import asyncio
import json
from datetime import date, datetime, timedelta

from shared.schemas import AgentResponse, DoseTakenToolRequest
from system_app.models import AgentDecisionAudit, AgentJob, ChatMessage, DoseEvent, Notification, ReminderPolicy, SystemPolicyOverride
from system_app.services.adherence_pattern_service import PATTERN_MESSAGES
from system_app.services.agent_callback_service import apply_agent_dose_taken_request
from system_app.services.agent_response_service import maybe_apply_dose_taken_response
from system_app.services.conversation_service import handle_policy_confirmation_message
from system_app.services.dose_event_service import ensure_day_events, missed_dose_pending_body
from system_app.services.side_effect_reminder_safety import set_reminder_suppressed_after_side_effect
from system_app.services.simulation import (
    build_daily_pattern,
    create_medication_plan,
    create_notification,
    delete_medication_plan,
    ensure_base_data,
    ensure_clock,
    get_latest_reply_prompt,
    handle_system_event,
    maybe_apply_policy_response,
    persist_agent_summary,
    prepare_notification_window,
    reset_simulation_state,
    resolve_choice_value,
    resolve_schedule_times,
    set_clock_state,
)
from system_app.services.system_request_service import build_multiturn_chat_request
from system_app.services.timeline_service import get_today_dose_events
from tests.helpers import build_session


class PolicyChangeSystemAgentClient:
    async def send_multiturn_chat(self, request):
        return AgentResponse(
            trace_id="trace-system-policy",
            agent_name="system_event_agent",
            prompt_version_id="system_event_agent_v11",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "apply_notification_policy",
                    "arguments": {
                        "slot_label": "아침 08:00",
                        "extra_reminders": 2,
                        "interval_minutes": 10,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "환자 대화 요청 반영",
                        "source": "patient_request",
                    },
                },
                "message": "아침 알림 정책을 변경했습니다.",
            },
            human_summary="아침 알림 정책을 변경했습니다.",
            requires_conversation_alert=False,
        )


class ClarifyingSystemPolicyAgentClient:
    async def send_multiturn_chat(self, request):
        return AgentResponse(
            trace_id="trace-system-clarify",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v3",
            decision_type="system_policy_clarification",
            structured_payload={
                "advice": "저녁이 현재 일정의 야간 21:00을 의미하나요?",
                "observations": ["정책 적용 전에 시간대 확인이 필요합니다."],
            },
            human_summary="저녁이 현재 일정의 야간 21:00을 의미하나요?",
            requires_conversation_alert=False,
        )


class SideEffectSystemAgentClient:
    async def send_multiturn_chat(self, request):
        return AgentResponse(
            trace_id="trace-side-effect",
            agent_name="system_event_agent",
            prompt_version_id="system_event_agent_v11",
            decision_type="side_effect_assessment",
            structured_payload={
                "tool_call": {
                    "name": "lookup_side_effect_info",
                    "arguments": {
                        "symptom_text": request.message,
                    },
                },
                "tool_results": [
                    {
                        "tool_name": "lookup_side_effect_info",
                        "status": "success",
                        "response": {
                            "suspected": True,
                            "matches": [{"symptom": "메스꺼움"}],
                        },
                    }
                ],
                "tools_executed": True,
            },
            human_summary="속 메스꺼움은 복용 중인 약과 관련 가능성이 있어요. 증상이 심하거나 지속되면 의료진이나 약사에게 확인해주세요.",
            requires_conversation_alert=False,
        )


def test_delete_medication_plan_removes_linked_records_and_unused_policies():
    with build_session() as session:
        plan = create_medication_plan(
            session,
            medication_name="검증용 혈압약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 22),
            times_csv="08:00,21:00",
            instructions="식후 복용",
        )
        events = session.query(DoseEvent).filter(DoseEvent.plan_id == plan.id).all()
        for event in events:
            create_notification(
                session,
                notification_type="medication_alert",
                title="복약 알림",
                body="테스트 알림",
                visible_at=event.scheduled_for,
                related_dose_event_id=event.id,
            )
            session.add(
                ChatMessage(
                    patient_id="demo-patient",
                    role="assistant",
                    sender_type="assistant",
                    category="missed_dose",
                    content="테스트 대화",
                    related_dose_event_id=event.id,
                )
            )

        session.add(
            ReminderPolicy(
                patient_id="demo-patient",
                slot_label="아침 08:00",
                extra_reminders=2,
                interval_minutes=5,
                effective_start_date=date(2026, 4, 20),
                effective_end_date=date(2026, 4, 27),
                reason="테스트 정책",
                source="pattern_analysis",
                active=True,
            )
        )
        session.add(
            ReminderPolicy(
                patient_id="demo-patient",
                slot_label="야간 21:00",
                extra_reminders=1,
                interval_minutes=5,
                effective_start_date=date(2026, 4, 20),
                effective_end_date=date(2026, 4, 27),
                reason="테스트 정책",
                source="pattern_analysis",
                active=True,
            )
        )
        session.commit()

        assert delete_medication_plan(session, plan.id) is True

        assert session.query(DoseEvent).count() == 0
        assert session.query(Notification).count() == 0
        assert session.query(ChatMessage).filter(ChatMessage.related_dose_event_id.is_not(None)).count() == 0
        assert session.query(ReminderPolicy).filter(ReminderPolicy.active.is_(True)).count() == 0


def test_delete_medication_plan_returns_false_for_unknown_plan():
    with build_session() as session:
        assert delete_medication_plan(session, 9999) is False


def test_reset_simulation_state_clears_runtime_data_and_restores_clock():
    with build_session() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 23, 12, 45)
        clock.is_running = True
        clock.speed_multiplier = 15

        plan = create_medication_plan(
            session,
            medication_name="리셋 검증약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 24),
            times_csv="08:00,21:00",
            instructions="테스트",
        )
        events = session.query(DoseEvent).filter(DoseEvent.plan_id == plan.id).all()
        for event in events:
            create_notification(
                session,
                notification_type="medication_alert",
                title="복약 알림",
                body="리셋 테스트",
                visible_at=event.scheduled_for,
                related_dose_event_id=event.id,
            )

        session.add(
            ChatMessage(
                patient_id="demo-patient",
                role="user",
                sender_type="patient",
                category="chat",
                content="테스트 메시지",
            )
        )
        session.add(
            ReminderPolicy(
                patient_id="demo-patient",
                slot_label="아침 08:00",
                extra_reminders=2,
                interval_minutes=5,
                effective_start_date=date(2026, 4, 20),
                effective_end_date=date(2026, 4, 27),
                reason="테스트 정책",
                source="pattern_analysis",
                active=True,
            )
        )
        session.add(
            AgentDecisionAudit(
                trace_id="reset-test",
                agent_name="planner",
                prompt_version_id="v1",
                decision_type="pattern_policy_recommendation",
                structured_payload="{}",
                human_summary="테스트 요약",
                applied=True,
                error_message="",
                source_event_type="daily_pattern",
            )
        )
        session.commit()

        reset_simulation_state(session)

        assert session.query(DoseEvent).count() == 0
        assert session.query(Notification).count() == 0
        assert session.query(ReminderPolicy).count() == 0
        assert session.query(AgentDecisionAudit).count() == 0
        assert session.query(ChatMessage).count() == 0

        clock = ensure_clock(session)
        assert clock.current_time == datetime(2026, 4, 20, 8, 0)
        assert clock.is_running is False
        assert clock.speed_multiplier == 0


def test_schedule_and_choice_resolution_supports_presets_and_custom_values():
    assert resolve_schedule_times("morning_evening") == "08:00, 21:00"
    assert resolve_choice_value("혈압약", "", "") == "혈압약"
    assert resolve_choice_value("__custom__", "직접입력약", "") == "직접입력약"


def test_set_clock_state_moves_time_without_processing_alert_window_immediately():
    with build_session() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        original_processed_time = clock.last_processed_sim_time

        import asyncio

        asyncio.run(set_clock_state(session, advance_minutes=30))

        clock = ensure_clock(session)
        assert clock.current_time == datetime(2026, 4, 20, 8, 30)
        assert clock.last_processed_sim_time == original_processed_time


def test_set_clock_state_prepares_next_day_events_for_timeline():
    with build_session() as session:
        create_medication_plan(
            session,
            medication_name="다일정 혈압약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 21),
            times_csv="08:00,21:00",
            instructions="테스트",
        )

        asyncio.run(set_clock_state(session, advance_minutes=24 * 60))
        clock = ensure_clock(session)
        rows = get_today_dose_events(session, clock.current_time)

        assert clock.current_time.date() == date(2026, 4, 21)
        assert [(row.scheduled_for.date(), row.scheduled_for.strftime("%H:%M")) for row in rows] == [
            (date(2026, 4, 21), "08:00"),
            (date(2026, 4, 21), "21:00"),
        ]


def test_conversation_alert_pauses_clock_and_sets_latest_reply_prompt():
    with build_session() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.is_running = True
        clock.speed_multiplier = 15
        session.commit()

        response = AgentResponse(
            trace_id="trace-conversation",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={},
            human_summary="방금 복약하지 않은 이유가 무엇인가요?",
            requires_conversation_alert=True,
            validation_passed=True,
            validation_errors=[],
        )

        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=None)
        session.commit()

        clock = ensure_clock(session)
        assert clock.is_running is False
        assert clock.speed_multiplier == 0
        notification = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        assert notification.title == "AI가 대화를 요청합니다."
        assert notification.body == "방금 복약하지 않은 이유가 무엇인가요?"
        assert '"was_running": true' in notification.metadata_json
        assert '"speed_multiplier": 15' in notification.metadata_json
        assert get_latest_reply_prompt(session, clock.current_time) == "방금 복약하지 않은 이유가 무엇인가요?"


def test_default_missed_dose_detection_waits_90_minutes_then_pauses_with_alert():
    with build_session() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.is_running = True
        clock.speed_multiplier = 15
        session.commit()

        create_medication_plan(
            session,
            medication_name="감지 검증약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )

        early_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 9, 29),
        )
        assert early_payloads == []
        notifications = session.query(Notification).filter(Notification.notification_type == "medication_alert").order_by(Notification.visible_at).all()
        assert [row.visible_at for row in notifications] == [
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 8, 30),
        ]

        missed_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 9, 29),
            datetime(2026, 4, 20, 9, 30),
        )

        assert len(missed_payloads) == 1
        assert missed_payloads[0].detected_at == datetime(2026, 4, 20, 9, 30)
        event = session.get(DoseEvent, missed_payloads[0].dose_event_id)
        assert event is not None
        assert event.missed_detected_at == datetime(2026, 4, 20, 9, 30)
        clock = ensure_clock(session)
        assert clock.current_time == datetime(2026, 4, 20, 9, 30)
        assert clock.last_processed_sim_time == datetime(2026, 4, 20, 9, 30)
        assert clock.is_running is False
        assert clock.speed_multiplier == 0
        notification = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        assert notification.visible_at == datetime(2026, 4, 20, 9, 30)
        assert "AI가 상황을 확인하고 있어요" in notification.body
        assert '"status": "awaiting_agent"' in notification.metadata_json


def test_side_effect_suppression_skips_medication_and_missed_dose_alerts():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )
        set_reminder_suppressed_after_side_effect(session, True)
        session.commit()

        missed_payloads, pattern_jobs = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 9, 30),
        )

        event = session.query(DoseEvent).one()
        assert missed_payloads == []
        assert pattern_jobs == []
        assert event.alerts_generated is True
        assert event.status == "missed"
        assert event.missed_handled is True
        assert event.missed_detected_at == datetime(2026, 4, 20, 9, 30)
        assert session.query(Notification).filter(Notification.notification_type == "medication_alert").count() == 0
        assert session.query(Notification).filter(Notification.notification_type == "conversation_alert").count() == 0


def test_missed_dose_pending_body_strips_legacy_reply_hints():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )
        event = session.query(DoseEvent).one()
        policy = type(
            "Policy",
            (),
            {
                "missed_dose_body_template": (
                    "{slot_label} {medication_name} 미복용이 확정되어 AI가 상황을 확인하고 있어요. "
                    "잠시 후 이 알림 안에서 답변할 수 있습니다. 잠시 후 채팅에서 답변할 수 있습니다."
                )
            },
        )()

        body = missed_dose_pending_body(session, event, policy)

        assert body == "아침 08:00 당뇨약 미복용이 확정되어 AI가 상황을 확인하고 있어요."
        assert "답변할 수 있습니다" not in body


def test_new_missed_dose_payload_does_not_reuse_previous_chat_context():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )
        session.add(
            ChatMessage(
                patient_id="demo-patient",
                role="user",
                sender_type="patient",
                category="missed_dose",
                content="매스꺼워요",
            )
        )
        session.commit()

        missed_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 9, 30),
        )

        assert len(missed_payloads) == 1
        assert missed_payloads[0].chat_context == []
        assert missed_payloads[0].conversation_context[-1].content == "매스꺼워요"


def test_daily_pattern_includes_missed_reply_and_recent_chat_context():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="13:00",
            instructions="테스트",
        )
        missed_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 13, 0),
            datetime(2026, 4, 20, 14, 30),
        )
        notification = (
            session.query(Notification)
            .filter(
                Notification.related_dose_event_id == missed_payloads[0].dose_event_id,
                Notification.notification_type == "conversation_alert",
            )
            .one()
        )
        metadata = json.loads(notification.metadata_json)
        metadata.update(
            {
                "category": "missed_dose",
                "status": "reply_completed",
                "patient_reply": "깜빡했어요.",
                "agent_reply": "점심 시간에 알림을 조정해볼까요?",
            }
        )
        notification.metadata_json = json.dumps(metadata, ensure_ascii=False)
        session.add(
            ChatMessage(
                patient_id="demo-patient",
                role="user",
                sender_type="patient",
                category="multiturn_chat",
                content="점심에는 회의가 많아요.",
            )
        )
        session.commit()

        pattern = build_daily_pattern(session, date(2026, 4, 20))

    assert pattern is not None
    assert pattern.missed_dose_reply_context[0].patient_reply == "깜빡했어요."
    assert pattern.missed_dose_reply_context[0].agent_reply == "점심 시간에 알림을 조정해볼까요?"
    assert pattern.conversation_context[-1].content == "점심에는 회의가 많아요."


def test_daily_pattern_payload_accumulates_rolling_seven_days():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 26),
            times_csv="08:00",
            instructions="테스트",
        )
        for index in range(7):
            target = date(2026, 4, 20) + timedelta(days=index)
            ensure_day_events(session, target)
        for event in session.query(DoseEvent).order_by(DoseEvent.scheduled_for.asc()).all():
            event.status = "missed" if event.scheduled_for.date() >= date(2026, 4, 24) else "taken"
            event.taken_at = event.scheduled_for if event.status == "taken" else None
            event.missed_handled = True
        session.commit()

        pattern = build_daily_pattern(session, date(2026, 4, 26))

    assert pattern is not None
    assert pattern.window_start_date == date(2026, 4, 20)
    assert pattern.window_end_date == date(2026, 4, 26)
    assert pattern.window_days == 7
    assert pattern.observed_day_count == 7
    assert len(pattern.dose_events) == 7
    assert pattern.slot_summaries[0].scheduled_count == 7
    assert pattern.slot_summaries[0].taken_count == 4
    assert pattern.slot_summaries[0].missed_count == 3


def test_daily_pattern_payload_uses_available_days_before_window_is_full():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 21),
            times_csv="08:00",
            instructions="테스트",
        )
        ensure_day_events(session, date(2026, 4, 20))
        ensure_day_events(session, date(2026, 4, 21))
        for event in session.query(DoseEvent).order_by(DoseEvent.scheduled_for.asc()).all():
            event.status = "taken"
            event.taken_at = event.scheduled_for
            event.missed_handled = True
        session.commit()

        pattern = build_daily_pattern(session, date(2026, 4, 21))

    assert pattern is not None
    assert pattern.window_start_date == date(2026, 4, 20)
    assert pattern.window_end_date == date(2026, 4, 21)
    assert pattern.window_days == 2
    assert pattern.observed_day_count == 2
    assert len(pattern.dose_events) == 2
    assert pattern.slot_summaries[0].scheduled_count == 2


def test_daily_pattern_payload_caps_older_history_to_recent_seven_days():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 29),
            times_csv="08:00",
            instructions="테스트",
        )
        for index in range(10):
            ensure_day_events(session, date(2026, 4, 20) + timedelta(days=index))
        for event in session.query(DoseEvent).order_by(DoseEvent.scheduled_for.asc()).all():
            event.status = "missed"
            event.missed_handled = True
        session.commit()

        pattern = build_daily_pattern(session, date(2026, 4, 29))

    assert pattern is not None
    assert pattern.window_start_date == date(2026, 4, 23)
    assert pattern.window_end_date == date(2026, 4, 29)
    assert pattern.window_days == 7
    assert pattern.observed_day_count == 7
    assert len(pattern.dose_events) == 7
    assert min(event.scheduled_for.date() for event in pattern.dose_events) == date(2026, 4, 23)
    assert max(event.scheduled_for.date() for event in pattern.dose_events) == date(2026, 4, 29)
    assert pattern.slot_summaries[0].scheduled_count == 7
    assert pattern.slot_summaries[0].missed_count == 7


def test_multiturn_agent_context_includes_latest_50_chat_messages():
    with build_session() as session:
        ensure_base_data(session)
        for index in range(60):
            session.add(
                ChatMessage(
                    patient_id="demo-patient",
                    role="user",
                    sender_type="patient",
                    category="multiturn_chat",
                    content=f"message-{index}",
                )
            )
        session.commit()

        request = build_multiturn_chat_request(session, "multiturn_chat", "최근 맥락 확인", request_notification_id=1)

        recent_chat = request.context["recent_chat"]
        assert len(recent_chat) == 50
        assert recent_chat[0]["content"] == "message-10"
        assert recent_chat[-1]["content"] == "message-59"


def create_taken_daily_pattern_plan(session):
    create_medication_plan(
        session,
        medication_name="당뇨약",
        dosage="1정",
        start_date=date(2026, 4, 20),
        end_date=date(2026, 4, 21),
        times_csv="08:00",
        instructions="테스트",
    )
    for event in session.query(DoseEvent).filter(DoseEvent.scheduled_for < datetime(2026, 4, 21)).all():
        event.status = "taken"
        event.taken_at = event.scheduled_for
        event.missed_handled = True
    session.commit()


def test_daily_pattern_waits_until_configured_conversation_time():
    with build_session() as session:
        ensure_base_data(session)
        create_taken_daily_pattern_plan(session)

        _missed_payloads, pattern_jobs = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 21, 8, 29),
        )

        assert pattern_jobs == []
        assert session.query(AgentJob).filter(AgentJob.job_type == "daily_pattern").count() == 0


def test_daily_pattern_runs_at_configured_conversation_time_once_and_pauses_clock():
    with build_session() as session:
        ensure_base_data(session)
        create_taken_daily_pattern_plan(session)
        clock = ensure_clock(session)
        clock.is_running = True
        clock.speed_multiplier = 60
        session.commit()

        _missed_payloads, pattern_jobs = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 21, 8, 30),
        )

        assert len(pattern_jobs) == 1
        assert pattern_jobs[0].date == date(2026, 4, 20)
        assert pattern_jobs[0].window_start_date == date(2026, 4, 20)
        assert pattern_jobs[0].window_end_date == date(2026, 4, 20)
        assert pattern_jobs[0].window_days == 1
        assert pattern_jobs[0].observed_day_count == 1
        assert pattern_jobs[0].slot_summaries[0].scheduled_count == 1
        clock = ensure_clock(session)
        assert clock.current_time == datetime(2026, 4, 21, 8, 30)
        assert clock.last_processed_sim_time == datetime(2026, 4, 21, 8, 30)
        assert clock.is_running is False
        assert clock.speed_multiplier == 0

        _missed_payloads, duplicate_pattern_jobs = prepare_notification_window(
            session,
            datetime(2026, 4, 21, 8, 30),
            datetime(2026, 4, 21, 8, 31),
        )
        assert duplicate_pattern_jobs == []


def test_side_effect_suppression_skips_daily_pattern_job_but_advances_cursor():
    with build_session() as session:
        ensure_base_data(session)
        create_taken_daily_pattern_plan(session)
        set_reminder_suppressed_after_side_effect(session, True)
        session.commit()

        _missed_payloads, pattern_jobs = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 21, 8, 30),
        )

        clock = ensure_clock(session)
        assert pattern_jobs == []
        assert clock.last_daily_pattern_sent_date == date(2026, 4, 20)
        assert session.query(Notification).filter(Notification.notification_type == "conversation_alert").count() == 0


def test_side_effect_suppression_does_not_build_daily_pattern_payload(monkeypatch):
    from system_app.services import dose_event_service

    with build_session() as session:
        ensure_base_data(session)
        create_taken_daily_pattern_plan(session)
        set_reminder_suppressed_after_side_effect(session, True)
        session.commit()

        def fail_build_daily_pattern(*args, **kwargs):
            raise AssertionError("daily pattern payload should not be built while reminders are suppressed")

        monkeypatch.setattr(dose_event_service, "build_daily_pattern", fail_build_daily_pattern)

        _missed_payloads, pattern_jobs = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 21, 8, 30),
        )

        assert pattern_jobs == []
        assert ensure_clock(session).last_daily_pattern_sent_date == date(2026, 4, 20)


def test_daily_pattern_null_cursor_starts_from_first_generated_day():
    with build_session() as session:
        ensure_base_data(session)
        create_taken_daily_pattern_plan(session)
        clock = ensure_clock(session)
        clock.last_daily_pattern_sent_date = None
        session.commit()

        _missed_payloads, pattern_jobs = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 21, 8, 30),
        )

        assert len(pattern_jobs) == 1
        assert pattern_jobs[0].date == date(2026, 4, 20)
        assert pattern_jobs[0].observed_day_count == 1
        assert ensure_clock(session).last_daily_pattern_sent_date == date(2026, 4, 20)


def test_daily_pattern_due_without_payload_does_not_pause_clock():
    with build_session() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.is_running = True
        clock.speed_multiplier = 60
        session.commit()

        _missed_payloads, pattern_jobs = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 21, 8, 30),
        )

        assert pattern_jobs == []
        clock = ensure_clock(session)
        assert clock.is_running is True
        assert clock.speed_multiplier == 60
        assert clock.last_daily_pattern_sent_date == date(2026, 4, 20)


def test_conversation_alert_uses_missed_due_time_after_large_time_jump():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )

        missed_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 19, 0),
        )
        assert len(missed_payloads) == 1
        assert missed_payloads[0].detected_at == datetime(2026, 4, 20, 9, 30)
        clock = ensure_clock(session)
        assert clock.current_time == datetime(2026, 4, 20, 9, 30)
        assert clock.last_processed_sim_time == datetime(2026, 4, 20, 9, 30)

        response = AgentResponse(
            trace_id="trace-late-render",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={},
            human_summary="아침 08:00에 드셔야 할 당뇨약을 놓치신 것 같아요.",
            requires_conversation_alert=True,
            validation_passed=True,
            validation_errors=[],
        )
        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=missed_payloads[0].dose_event_id)
        session.commit()

        notification = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        assert notification.visible_at == datetime(2026, 4, 20, 9, 30)
        assert notification.body == PATTERN_MESSAGES["B"]
        assert "당뇨약" not in notification.body
        assert '"status": "agent_ready"' in notification.metadata_json
        assert session.query(Notification).filter(Notification.notification_type == "conversation_alert").count() == 1


def test_missed_dose_agent_summary_closes_duplicate_pending_alerts():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="21:00",
            instructions="테스트",
        )
        missed_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 21, 0),
            datetime(2026, 4, 20, 22, 30),
        )
        dose_event_id = missed_payloads[0].dose_event_id
        create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="야간 21:00 당뇨약 미복용이 확정되어 AI가 상황을 확인하고 있어요.",
            visible_at=datetime(2026, 4, 20, 22, 30),
            related_dose_event_id=dose_event_id,
            metadata={"category": "missed_dose", "status": "awaiting_agent"},
        )

        response = AgentResponse(
            trace_id="trace-duplicate-alert",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={},
            human_summary="오늘 밤 21:00 당뇨약 복약을 놓치신 것으로 확인되었습니다. 현재 복용 가능하신가요?",
            requires_conversation_alert=True,
        )
        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=dose_event_id)
        session.commit()

        alerts = (
            session.query(Notification)
            .filter(Notification.notification_type == "conversation_alert", Notification.related_dose_event_id == dose_event_id)
            .order_by(Notification.id.asc())
            .all()
        )
        active_alerts = [row for row in alerts if not row.acknowledged]
        assert len(alerts) == 2
        assert len(active_alerts) == 1
        assert active_alerts[0].body == PATTERN_MESSAGES["B"]
        assert "당뇨약" not in active_alerts[0].body
        assert json.loads(active_alerts[0].metadata_json)["status"] == "agent_ready"
        assert any(row.acknowledged for row in alerts)
        assert session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").count() == 1


def test_missed_dose_response_updates_pending_alert_even_without_alert_flag():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )

        missed_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 9, 30),
        )
        assert len(missed_payloads) == 1
        pending_alert = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        assert '"status": "awaiting_agent"' in pending_alert.metadata_json

        response = AgentResponse(
            trace_id="trace-ready-without-flag",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={},
            human_summary="아침 08:00 당뇨약을 놓친 이유를 알려주세요.",
            requires_conversation_alert=False,
            validation_passed=True,
            validation_errors=[],
        )
        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=missed_payloads[0].dose_event_id)
        session.commit()

        notification = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        assert notification.id == pending_alert.id
        assert notification.body == PATTERN_MESSAGES["B"]
        assert '"status": "agent_ready"' in notification.metadata_json
        assert get_latest_reply_prompt(session, ensure_clock(session).current_time) == PATTERN_MESSAGES["B"]
        chat_message = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one()
        chat_metadata = json.loads(chat_message.metadata_json)
        assert chat_message.content == PATTERN_MESSAGES["B"]
        assert chat_metadata["adherence_pattern"]["pattern_code"] == "B"
        assert chat_metadata["adherence_pattern"]["message_validation"]["passed"] is True


def test_missed_dose_response_with_empty_summary_still_creates_chat_prompt():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="당뇨약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )

        missed_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 9, 30),
        )
        response = AgentResponse(
            trace_id="trace-empty-summary",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={},
            human_summary="",
            requires_conversation_alert=True,
            validation_passed=True,
            validation_errors=[],
        )
        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=missed_payloads[0].dose_event_id)
        session.commit()

        notification = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        chat_message = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one()

        assert notification.body == PATTERN_MESSAGES["B"]
        chat_metadata = json.loads(chat_message.metadata_json)
        assert chat_message.content == PATTERN_MESSAGES["B"]
        assert chat_metadata["adherence_pattern"]["pattern_code"] == "B"
        assert chat_metadata["conversation_alert"]["notification_id"] == notification.id
        assert f'"notification_id": {notification.id}' in chat_message.metadata_json


def test_custom_policy_controls_extra_reminders_and_missed_dose_delay():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="정책 검증약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )
        session.add(
            ReminderPolicy(
                patient_id="demo-patient",
                slot_label="아침 08:00",
                extra_reminders=2,
                interval_minutes=5,
                effective_start_date=date(2026, 4, 20),
                effective_end_date=date(2026, 4, 20),
                reason="사용자 요청에 따라 짧은 반복 알림",
                source="patient_request",
                active=True,
            )
        )
        session.commit()

        early_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 8, 14),
        )
        assert early_payloads == []
        notifications = session.query(Notification).filter(Notification.notification_type == "medication_alert").order_by(Notification.visible_at).all()
        assert [row.visible_at for row in notifications] == [
            datetime(2026, 4, 20, 8, 0),
            datetime(2026, 4, 20, 8, 5),
            datetime(2026, 4, 20, 8, 10),
        ]

        missed_payloads, _ = prepare_notification_window(
            session,
            datetime(2026, 4, 20, 8, 14),
            datetime(2026, 4, 20, 8, 15),
        )

        assert len(missed_payloads) == 1


def test_handle_system_event_asks_for_confirmation_before_policy_change():
    with build_session() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 0)
        session.commit()

        asyncio.run(
            handle_system_event(
                session,
                PolicyChangeSystemAgentClient(),
                "multiturn_chat",
                "아침 알림을 2번 10분 간격으로 바꿔줘",
            )
        )

        assert session.query(ReminderPolicy).filter(ReminderPolicy.source == "patient_request").count() == 0
        confirmation = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        confirmation_metadata = json.loads(confirmation.metadata_json)
        assert confirmation.title == "AI가 대화를 요청합니다."
        assert confirmation.body == "아침 08:00의 복약 알림을 추가 2회, 10분 간격으로 변경하시는 건 어떨까요?"
        assert "선택해주세요" not in confirmation.body
        assert "제안하겠습니다" not in confirmation.body
        assert "확인 알림" not in confirmation.body
        assert confirmation_metadata["category"] == "policy_confirmation"
        assert confirmation_metadata["recommended_action"] == "increase"
        assert confirmation_metadata["confirmation_options"] == ["1. 늘리기", "2. 현행 유지"]
        assert [option["value"] for option in confirmation_metadata["multiple_choice"]["options"]] == ["increase", "keep"]
        policy_change = confirmation_metadata["policy_change"]
        assert policy_change["question"] == confirmation.body
        assert policy_change["candidates"][0]["slot_label"] == "아침 08:00"
        assert policy_change["candidates"][0]["proposed"]["summary"] == "추가 2회 · 10분 간격 · 미복용 90분 후 · 정시 알림"

        applied, result_message = handle_policy_confirmation_message(session, "1", notification_id=confirmation.id)

        policy = session.query(ReminderPolicy).filter(ReminderPolicy.source == "patient_request").one()
        assert applied is True
        assert result_message == "아침 08:00: 정책이 적용되었습니다."
        assert policy.slot_label == "아침 08:00"
        assert policy.extra_reminders == 2
        assert policy.interval_minutes == 10
        assert policy.active is True
        assistant_message = (
            session.query(ChatMessage)
            .filter(
                ChatMessage.role == "assistant",
                ChatMessage.category == "policy_confirmation",
                ChatMessage.content == "아침 08:00의 복약 알림을 추가 2회, 10분 간격으로 변경하시는 건 어떨까요?",
            )
            .one()
        )
        assert assistant_message.content == "아침 08:00의 복약 알림을 추가 2회, 10분 간격으로 변경하시는 건 어떨까요?"
        assert "예정" not in assistant_message.content
        assert "변경했습니다" not in assistant_message.content
        assert assistant_message.category == "policy_confirmation"
        notification = session.query(Notification).filter(Notification.notification_type == "system_policy_request").one()
        metadata = json.loads(notification.metadata_json)
        assert notification.title == "정책 변경 확인 대기"
        assert "정책 적용 전에 환자 확인이 필요합니다" in notification.body
        assert metadata["status"] == "needs_confirmation"
        assert metadata["request_message"] == "아침 알림을 2번 10분 간격으로 바꿔줘"
        assert metadata["result_message"] == "정책 변경 후보를 채팅에 표시했습니다."


def test_handle_system_event_keeps_ambiguous_policy_request_as_clarification():
    with build_session() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 30)
        session.commit()

        asyncio.run(
            handle_system_event(
                session,
                ClarifyingSystemPolicyAgentClient(),
                "multiturn_chat",
                "난 저녁에는 5회 1분 간격으로 알려줬으면 좋겠어",
            )
        )

        assert session.query(ReminderPolicy).filter(ReminderPolicy.source == "patient_request").count() == 0
        notification = session.query(Notification).filter(Notification.notification_type == "system_policy_request").one()
        metadata = json.loads(notification.metadata_json)
        assert notification.title == "에이전트 추가 확인 필요"
        assert "야간 21:00" in notification.body
        assert metadata["status"] == "needs_clarification"
        assert metadata["result_message"] == "저녁이 현재 일정의 야간 21:00을 의미하나요?"


def test_handle_system_event_side_effect_question_completes_pending_request():
    with build_session() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 12, 45)
        session.commit()

        asyncio.run(
            handle_system_event(
                session,
                SideEffectSystemAgentClient(),
                "multiturn_chat",
                "속이 매스꺼운데 약때문일까?",
            )
        )

        notification = session.query(Notification).filter(Notification.notification_type == "system_policy_request").one()
        metadata = json.loads(notification.metadata_json)
        assistant_message = session.query(ChatMessage).filter(ChatMessage.role == "assistant", ChatMessage.category == "multiturn_chat").one()

        assert metadata["status"] == "answered"
        assert "속 메스꺼움" in metadata["result_message"]
        assert notification.title == "에이전트 답변 완료"
        assert "속 메스꺼움" in assistant_message.content
        assert session.query(Notification).filter(Notification.notification_type == "agent_error").count() == 0


def test_daily_pattern_policy_response_creates_confirmation_then_applies_reply():
    with build_session() as session:
        ensure_base_data(session)
        response = AgentResponse(
            trace_id="trace-multi-policy",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v1",
            decision_type="pattern_policy_recommendation",
            structured_payload={
                "policy_deltas": [
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
                    {
                        "slot_label": "야간 21:00",
                        "extra_reminders": 2,
                        "interval_minutes": 10,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "야간 누락률이 높습니다.",
                        "source": "pattern_analysis",
                    },
                ]
            },
            human_summary="세 시간대 알림 정책을 강화합니다.",
            requires_conversation_alert=False,
        )

        applied, message = maybe_apply_policy_response(session, response, "daily_pattern")
        session.commit()

        policies = session.query(ReminderPolicy).filter(ReminderPolicy.source == "pattern_analysis").order_by(ReminderPolicy.slot_label).all()
        audits = session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == "trace-multi-policy").order_by(AgentDecisionAudit.id).all()
        confirmation = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        metadata = json.loads(confirmation.metadata_json)
        assert applied is False
        assert message == "정책 변경 후보를 채팅에 표시했습니다."
        assert policies == []
        assert confirmation.body == "아침, 점심, 야간 모든 시간대의 복약 알림을 아래 후보처럼 변경하시는 건 어떨까요?"
        assert metadata["category"] == "policy_confirmation"
        assert len(metadata["proposed_policies"]) == 3
        assert len(metadata["policy_change"]["candidates"]) == 3
        assert metadata["recommended_action"] == "increase"
        assert metadata["confirmation_options"] == ["1. 늘리기", "2. 현행 유지"]
        assert len(audits) == 1

        applied, message = handle_policy_confirmation_message(session, "increase", notification_id=confirmation.id)
        session.commit()

        policies = session.query(ReminderPolicy).filter(ReminderPolicy.source == "pattern_analysis").order_by(ReminderPolicy.slot_label).all()
        audits = session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == "trace-multi-policy").order_by(AgentDecisionAudit.id).all()
        assert applied is True
        assert message == "아침 08:00: 정책이 적용되었습니다. / 점심 13:00: 정책이 적용되었습니다. / 야간 21:00: 정책이 적용되었습니다."
        assert [policy.slot_label for policy in policies] == ["아침 08:00", "야간 21:00", "점심 13:00"]
        assert [policy.extra_reminders for policy in policies] == [2, 2, 2]
        assert len(audits) == 4
        assert [json.loads(audit.structured_payload).get("tool_call", {}).get("arguments", {}).get("slot_label") for audit in audits[1:]] == [
            "아침 08:00",
            "점심 13:00",
            "야간 21:00",
        ]


def test_system_policy_tool_call_creates_confirmation_then_applies_reply():
    with build_session() as session:
        ensure_base_data(session)
        response = AgentResponse(
            trace_id="trace-system-policy-confirmation",
            agent_name="system_event_agent",
            prompt_version_id="agent_app_v2_tool_runtime",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "apply_system_policy",
                    "arguments": {
                        "policy_key": "daily_pattern_conversation_time",
                        "value": "09:20",
                        "reason": "환자가 일일 패턴 대화 요청 시간을 09:20으로 변경 요청했습니다.",
                        "source": "patient_request",
                    },
                },
                "tool_results": [
                    {
                        "tool_name": "apply_system_policy",
                        "status": "skipped",
                        "response": {
                            "tool_name": "apply_system_policy",
                            "reason": "policy_confirmation_required",
                            "policy_key": "daily_pattern_conversation_time",
                        },
                    }
                ],
                "tools_executed": False,
                "policy_confirmation_required": True,
            },
            human_summary="시스템 정책 변경 후보를 만들었습니다. 확인 후 반영됩니다.",
            requires_conversation_alert=False,
        )

        applied, message = maybe_apply_policy_response(session, response, "multiturn_chat")
        session.commit()

        confirmation = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        metadata = json.loads(confirmation.metadata_json)
        assert applied is False
        assert message == "정책 변경 후보를 채팅에 표시했습니다."
        assert session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == "daily_pattern_conversation_time").count() == 0
        assert metadata["category"] == "policy_confirmation"
        assert metadata["policy_change"]["type"] == "system_policy"
        assert metadata["proposed_system_policies"][0]["value"] == "09:20"
        assert metadata["confirmation_options"] == ["1. 적용하기", "2. 현행 유지"]

        applied, result_message = handle_policy_confirmation_message(session, "1", notification_id=confirmation.id)
        session.commit()

        override = session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == "daily_pattern_conversation_time").one()
        assert applied is True
        assert "daily_pattern_conversation_time: 일일 패턴 대화 요청 시간을 09:20로 변경했습니다." == result_message
        assert override.value == "09:20"
        assert override.active is True


def test_system_policy_confirmation_keep_current_does_not_apply_override():
    with build_session() as session:
        ensure_base_data(session)
        response = AgentResponse(
            trace_id="trace-system-policy-keep",
            agent_name="system_event_agent",
            prompt_version_id="agent_app_v2_tool_runtime",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "apply_system_policy",
                    "arguments": {
                        "policy_key": "daily_pattern_conversation_time",
                        "value": "09:20",
                        "reason": "환자 요청",
                        "source": "patient_request",
                    },
                },
                "tools_executed": False,
            },
            human_summary="시스템 정책 변경 후보를 만들었습니다.",
            requires_conversation_alert=False,
        )

        maybe_apply_policy_response(session, response, "multiturn_chat")
        confirmation = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        applied, result_message = handle_policy_confirmation_message(session, "2", notification_id=confirmation.id)

        assert applied is False
        assert result_message == "현행 시스템 정책을 유지했습니다."
        assert session.query(SystemPolicyOverride).filter(SystemPolicyOverride.policy_key == "daily_pattern_conversation_time").count() == 0


def test_unexecuted_mark_dose_taken_tool_call_does_not_change_dose_event():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="혈압약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )
        event = session.query(DoseEvent).one()
        response = AgentResponse(
            trace_id="trace-unexecuted-dose-tool",
            agent_name="system_event_agent",
            prompt_version_id="agent_app_v2_tool_runtime",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "mark_dose_taken",
                    "arguments": {
                        "dose_event_id": event.id,
                        "reason": "patient_reported_taken",
                    },
                },
                "tools_executed": False,
            },
            human_summary="복약 완료를 기록하겠습니다.",
            requires_conversation_alert=False,
        )

        applied, result_message = maybe_apply_dose_taken_response(session, response, "multiturn_chat")
        session.flush()

        event = session.get(DoseEvent, event.id)
        assert applied is False
        assert result_message == "실행된 복약 완료 tool result가 없습니다."
        assert event is not None
        assert event.status != "taken"
        assert event.taken_at is None


def test_agent_dose_taken_callback_still_marks_event_taken():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="혈압약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
            instructions="테스트",
        )
        event = session.query(DoseEvent).one()

        result = apply_agent_dose_taken_request(
            session,
            DoseTakenToolRequest(
                dose_event_id=event.id,
                reason="patient_reported_taken",
                source_trace_id="trace-dose-callback",
                source_event_type="multiturn_chat",
            ),
        )

        event = session.get(DoseEvent, event.id)
        assert result.status == "taken"
        assert event is not None
        assert event.status == "taken"
        assert event.taken_at is not None


def test_policy_tool_call_with_tool_results_still_requires_confirmation():
    with build_session() as session:
        ensure_base_data(session)
        response = AgentResponse(
            trace_id="trace-tool-policy-confirmation",
            agent_name="daily_pattern_agent",
            prompt_version_id="agent_app_v2_tool_runtime",
            decision_type="tool_call",
            structured_payload={
                "tool_calls": [
                    {
                        "name": "apply_notification_policy",
                        "arguments": {
                            "slot_label": "아침 08:00",
                            "extra_reminders": 2,
                            "interval_minutes": 20,
                            "effective_start_date": "2026-04-21",
                            "effective_end_date": "2026-05-20",
                            "reason": "complete_daily_miss",
                            "source": "ai_adherence_analysis",
                        },
                    },
                    {
                        "name": "apply_notification_policy",
                        "arguments": {
                            "slot_label": "점심 13:00",
                            "extra_reminders": 2,
                            "interval_minutes": 20,
                            "effective_start_date": "2026-04-21",
                            "effective_end_date": "2026-05-20",
                            "reason": "complete_daily_miss",
                            "source": "ai_adherence_analysis",
                        },
                    },
                    {
                        "name": "apply_notification_policy",
                        "arguments": {
                            "slot_label": "야간 21:00",
                            "extra_reminders": 2,
                            "interval_minutes": 20,
                            "effective_start_date": "2026-04-21",
                            "effective_end_date": "2026-05-20",
                            "reason": "complete_daily_miss",
                            "source": "ai_adherence_analysis",
                        },
                    },
                ],
                "tool_results": [
                    {
                        "tool_name": "apply_notification_policy",
                        "status": "success",
                        "response": {"results": [{"slot_label": "아침 08:00", "applied": True, "message": "정책이 적용되었습니다."}]},
                    }
                ],
                "tools_executed": True,
            },
            human_summary="하루 전체 알림 정책 변경 후보를 만들었습니다.",
            requires_conversation_alert=False,
        )

        applied, message = maybe_apply_policy_response(session, response, "daily_pattern")
        session.commit()

        policies = session.query(ReminderPolicy).filter(ReminderPolicy.source == "pattern_analysis").all()
        confirmation = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        metadata = json.loads(confirmation.metadata_json)
        assert applied is False
        assert message == "정책 변경 후보를 채팅에 표시했습니다."
        assert policies == []
        assert len(metadata["proposed_policies"]) == 3
        assert [policy["slot_label"] for policy in metadata["proposed_policies"]] == ["아침 08:00", "점심 13:00", "야간 21:00"]
        assert {policy["source"] for policy in metadata["proposed_policies"]} == {"pattern_analysis"}


def test_policy_response_with_no_effective_change_does_not_create_confirmation():
    with build_session() as session:
        ensure_base_data(session)
        for slot_label in ["아침 08:00", "점심 13:00", "야간 21:00"]:
            session.add(
                ReminderPolicy(
                    patient_id="demo-patient",
                    slot_label=slot_label,
                    extra_reminders=2,
                    interval_minutes=10,
                    missed_dose_after_minutes=90,
                    primary_reminder_timing="at",
                    primary_reminder_offset_minutes=0,
                    effective_start_date=date(2026, 4, 20),
                    effective_end_date=date(2026, 4, 27),
                    reason="이미 적용된 강화 정책",
                    source="pattern_analysis",
                    active=True,
                )
            )
        session.commit()
        response = AgentResponse(
            trace_id="trace-no-effective-change",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v1",
            decision_type="pattern_policy_recommendation",
            structured_payload={
                "policy_deltas": [
                    {
                        "slot_label": slot_label,
                        "extra_reminders": 2,
                        "interval_minutes": 10,
                        "missed_dose_after_minutes": 90,
                        "primary_reminder_timing": "at",
                        "primary_reminder_offset_minutes": 0,
                        "effective_start_date": "2026-04-21",
                        "effective_end_date": "2026-04-28",
                        "reason": "이미 현재 정책과 같은 후보입니다.",
                        "source": "pattern_analysis",
                    }
                    for slot_label in ["아침 08:00", "점심 13:00", "야간 21:00"]
                ]
            },
            human_summary="각 시간대마다 알림을 10분 간격으로 2회 더 받아보시는 건 어떨까요?",
            requires_conversation_alert=False,
        )

        applied, message = maybe_apply_policy_response(session, response, "daily_pattern")

        assert applied is False
        assert message == "현재 정책과 같은 제안이라 확인 알림을 만들지 않았습니다."
        assert session.query(Notification).filter(Notification.notification_type == "conversation_alert").count() == 0
        audit = session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == "trace-no-effective-change").one()
        assert audit.applied is False
        assert audit.error_message == "policy_confirmation_skipped:no_effective_change"


def test_daily_pattern_recommendation_without_policy_delta_does_not_fail():
    with build_session() as session:
        ensure_base_data(session)
        response = AgentResponse(
            trace_id="trace-daily-guidance-only",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v14",
            decision_type="pattern_policy_recommendation",
            structured_payload={
                "advice": "최근 기록을 확인했지만 바로 변경할 정책 후보는 없습니다.",
                "policy_action": "keep_current",
                "reason_category": "already_optimized",
                "observations": ["정책 도구 호출 없음"],
                "tools_executed": False,
            },
            human_summary="최근 기록을 확인했지만 바로 변경할 정책 후보는 없습니다.",
            requires_conversation_alert=False,
        )

        applied, message = maybe_apply_policy_response(session, response, "daily_pattern")

        assert applied is False
        assert message == "최근 기록을 확인했지만 바로 변경할 정책 후보는 없습니다."
        assert session.query(Notification).filter(Notification.notification_type == "conversation_alert").count() == 0
        audit = session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == "trace-daily-guidance-only").one()
        assert audit.applied is False
        assert audit.error_message == ""


def test_policy_confirmation_keep_current_does_not_apply_policy():
    with build_session() as session:
        ensure_base_data(session)
        response = AgentResponse(
            trace_id="trace-keep-policy",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v4",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "apply_notification_policy",
                    "arguments": {
                        "slot_label": "아침 08:00",
                        "extra_reminders": 2,
                        "interval_minutes": 5,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "아침 누락률이 높습니다.",
                        "source": "pattern_analysis",
                    },
                }
            },
            human_summary="아침 알림 정책을 강화합니다.",
        )

        maybe_apply_policy_response(session, response, "daily_pattern")
        confirmation = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        metadata = json.loads(confirmation.metadata_json)
        assert metadata["confirmation_options"] == ["1. 늘리기", "2. 현행 유지"]
        applied, result_message = handle_policy_confirmation_message(session, "2", notification_id=confirmation.id)

        assert applied is False
        assert result_message == "현행 알림 정책을 유지했습니다."
        assert session.query(ReminderPolicy).count() == 0


def test_policy_confirmation_ambiguous_policy_offer_three_options():
    with build_session() as session:
        ensure_base_data(session)
        response = AgentResponse(
            trace_id="trace-ambiguous-policy",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v4",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "apply_notification_policy",
                    "arguments": {
                        "slot_label": "아침 08:00",
                            "extra_reminders": 2,
                            "interval_minutes": 60,
                            "missed_dose_after_minutes": 180,
                            "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "알림 횟수와 간격 판단이 엇갈립니다.",
                        "source": "pattern_analysis",
                    },
                }
            },
            human_summary="아침 알림 정책 조정이 필요할 수 있습니다.",
        )

        maybe_apply_policy_response(session, response, "daily_pattern")
        confirmation = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        metadata = json.loads(confirmation.metadata_json)

        assert metadata["recommended_action"] is None
        assert metadata["confirmation_options"] == ["1. 늘리기", "2. 줄이기", "3. 현행 유지"]
        assert [option["value"] for option in metadata["multiple_choice"]["options"]] == ["increase", "decrease", "keep"]


def test_policy_confirmation_decrease_recommendation_applies_candidate_policy():
    with build_session() as session:
        ensure_base_data(session)
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
            trace_id="trace-decrease-policy",
            agent_name="policy_planner",
            prompt_version_id="policy_planner_v4",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "apply_notification_policy",
                    "arguments": {
                        "slot_label": "아침 08:00",
                        "extra_reminders": 2,
                        "interval_minutes": 10,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "기존 알림 부담을 줄입니다.",
                        "source": "pattern_analysis",
                    },
                }
            },
            human_summary="아침 알림 정책을 줄입니다.",
        )

        maybe_apply_policy_response(session, response, "daily_pattern")
        confirmation = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        metadata = json.loads(confirmation.metadata_json)
        assert metadata["recommended_action"] == "decrease"
        assert metadata["confirmation_options"] == ["1. 줄이기", "2. 현행 유지"]
        applied, result_message = handle_policy_confirmation_message(session, "1", notification_id=confirmation.id)

        policy = session.query(ReminderPolicy).filter(ReminderPolicy.active.is_(True)).one()
        assert applied is True
        assert result_message == "아침 08:00: 정책이 적용되었습니다."
        assert policy.extra_reminders == 2
        assert policy.interval_minutes == 10
        assert policy.reason == "기존 알림 부담을 줄입니다."
