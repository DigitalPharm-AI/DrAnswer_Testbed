import json
from datetime import date, datetime, timedelta

from shared.schemas import AgentResponse
from system_app.models import ChatMessage, DoseEvent, DoseSchedule, MedicationPlan, Notification
from system_app.services.adherence_pattern_service import (
    CLINICIAN_ESCALATION_TYPE,
    PATTERN_MESSAGES,
    build_streak_metrics,
    create_clinician_escalation_stub,
    evaluate_adherence_pattern,
    validate_pattern_message,
)
from system_app.services.agent_response_service import persist_agent_summary
from system_app.services.dose_event_service import create_missed_dose_conversation_alert
from system_app.services.notification_service import create_notification
from system_app.services.side_effect_reminder_safety import SIDE_EFFECT_REMINDER_SAFETY_CATEGORY, set_reminder_suppressed_after_side_effect
from system_app.services.tone_policy_service import all_tone_message_candidates, select_tone_policy
from tests.helpers import build_session


def seed_slot_events(
    session,
    statuses,
    *,
    start_date=date(2026, 4, 1),
    slot_label="아침 08:00",
    scheduled_time="08:00",
    medication_name="검증약",
):
    plan = MedicationPlan(
        patient_id="demo-patient",
        medication_name=medication_name,
        dosage="1정",
        start_date=start_date,
        end_date=start_date + timedelta(days=len(statuses) - 1),
        active=True,
    )
    session.add(plan)
    session.flush()
    schedule = DoseSchedule(plan_id=plan.id, slot_label=slot_label, scheduled_time=scheduled_time)
    session.add(schedule)
    session.flush()
    events = []
    scheduled_at = datetime.strptime(scheduled_time, "%H:%M").time()
    for index, status in enumerate(statuses):
        scheduled_for = datetime.combine(start_date + timedelta(days=index), scheduled_at)
        event = DoseEvent(
            patient_id="demo-patient",
            plan_id=plan.id,
            schedule_id=schedule.id,
            medication_name=medication_name,
            slot_label=slot_label,
            scheduled_for=scheduled_for,
            status=status,
            taken_at=scheduled_for + timedelta(minutes=3) if status == "taken" else None,
            missed_detected_at=scheduled_for + timedelta(minutes=90) if status == "missed" else None,
            missed_handled=status == "missed",
        )
        session.add(event)
        events.append(event)
    session.flush()
    return events


def test_pattern_a_simple_forgetfulness_after_long_taken_streak():
    with build_session() as session:
        current = seed_slot_events(session, ["taken"] * 14 + ["missed"])[-1]

        decision = evaluate_adherence_pattern(session, current)

        assert decision.pattern_code == "A"
        assert decision.streak_metrics.current_consecutive_missed_days == 1
        assert decision.streak_metrics.previous_consecutive_taken_days == 14
        assert current.medication_name not in decision.message


def test_pattern_b_habit_not_formed_for_short_taken_streak():
    with build_session() as session:
        current = seed_slot_events(session, ["taken", "taken", "missed"])[-1]

        decision = evaluate_adherence_pattern(session, current)

        assert decision.pattern_code == "B"
        assert decision.streak_metrics.previous_consecutive_taken_days == 2


def test_pattern_c_side_effect_keep_has_highest_priority():
    with build_session() as session:
        current = seed_slot_events(session, ["missed", "missed", "missed"])[-1]
        create_notification(
            session,
            notification_type="conversation_alert",
            title="부작용 기록 후 알림 확인",
            body="알림을 유지합니다.",
            visible_at=current.missed_detected_at - timedelta(days=1),
            metadata={
                "category": SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
                "status": "reply_completed",
                "action": "keep",
            },
        )

        decision = evaluate_adherence_pattern(session, current)

        assert decision.pattern_code == "C"
        assert decision.streak_metrics.current_consecutive_missed_days == 3
        assert decision.escalation_stub_required is False


def test_pattern_c_requires_keep_state_not_suppression():
    with build_session() as session:
        current = seed_slot_events(session, ["missed", "missed", "missed"])[-1]
        create_notification(
            session,
            notification_type="conversation_alert",
            title="부작용 기록 후 알림 확인",
            body="알림을 유지합니다.",
            visible_at=current.missed_detected_at - timedelta(days=1),
            metadata={
                "category": SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
                "status": "reply_completed",
                "action": "keep",
            },
        )
        set_reminder_suppressed_after_side_effect(session, True)

        decision = evaluate_adherence_pattern(session, current)

        assert decision.pattern_code == "D"
        assert decision.streak_metrics.recent_side_effect_keep is False


def test_pattern_d_long_consecutive_missed_creates_single_escalation_stub():
    with build_session() as session:
        current = seed_slot_events(session, ["missed", "missed", "missed"])[-1]
        decision = evaluate_adherence_pattern(session, current)

        first = create_clinician_escalation_stub(session, current, decision)
        second = create_clinician_escalation_stub(session, current, decision)

        stubs = session.query(Notification).filter(Notification.notification_type == CLINICIAN_ESCALATION_TYPE).all()
        metadata = json.loads(first.metadata_json)
        assert decision.pattern_code == "D"
        assert first.id == second.id
        assert len(stubs) == 1
        assert metadata["delivery_channel"] == "internal_only"
        assert metadata["pattern_code"] == "D"


def test_pattern_e_overall_low_adherence_with_two_day_current_streak():
    statuses = ["missed", "taken"] * 7 + ["missed", "missed"]
    with build_session() as session:
        current = seed_slot_events(session, statuses)[-1]

        decision = evaluate_adherence_pattern(session, current)

        assert decision.pattern_code == "E"
        assert decision.streak_metrics.current_consecutive_missed_days == 2
        assert decision.streak_metrics.overall_adherence_rate < 0.5
        assert decision.streak_metrics.prescription_day_count >= 15


def test_streak_is_slot_scoped_and_ignores_taken_other_slot():
    with build_session() as session:
        morning = seed_slot_events(session, ["missed", "missed"], slot_label="아침 08:00", scheduled_time="08:00")
        seed_slot_events(session, ["taken", "taken"], slot_label="야간 21:00", scheduled_time="21:00")

        metrics = build_streak_metrics(session, morning[-1])

        assert metrics.current_consecutive_missed_days == 2
        assert metrics.slot_scheduled_count == 2


def test_pattern_messages_pass_safety_validation():
    for code, message in PATTERN_MESSAGES.items():
        is_valid, errors = validate_pattern_message(message, medication_name="타목시펜")

        assert is_valid, f"{code}: {errors}"
        assert "타목시펜" not in message
        assert "반드시" not in message
        assert message.count("!") + message.count("?") <= 1


def test_tone_message_catalog_passes_safety_validation():
    for candidate in all_tone_message_candidates():
        is_valid, errors = validate_pattern_message(candidate.message, medication_name="타목시펜")

        assert is_valid, f"{candidate.pattern_code}/{candidate.tone_key}/{candidate.variant_key}: {errors}"


def test_validate_pattern_message_rejects_unsafe_text():
    is_valid, errors = validate_pattern_message("타목시펜은 반드시 복용하세요!!", medication_name="타목시펜")

    assert is_valid is False
    assert "forbidden_term:타목시펜" in errors
    assert "forbidden_term:반드시" in errors
    assert "too_many_punctuation_marks" in errors


def test_missed_dose_agent_ready_chat_uses_pattern_message_and_metadata():
    with build_session() as session:
        current = seed_slot_events(session, ["missed", "missed", "missed"], medication_name="혈압약")[-1]
        create_missed_dose_conversation_alert(session, current, current.missed_detected_at)
        response = AgentResponse(
            trace_id="trace-adherence-pattern-chat",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={},
            human_summary="아침 08:00 혈압약 복약을 놓친 이유를 알려주세요.",
            requires_conversation_alert=True,
        )

        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=current.id)

        message = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one()
        metadata = json.loads(message.metadata_json)
        alert = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        alert_metadata = json.loads(alert.metadata_json)
        stubs = session.query(Notification).filter(Notification.notification_type == CLINICIAN_ESCALATION_TYPE).all()
        assert message.content == PATTERN_MESSAGES["D"]
        assert alert_metadata["agent_response_preview"] == PATTERN_MESSAGES["D"]
        assert "혈압약" not in message.content
        assert metadata["adherence_pattern"]["pattern_code"] == "D"
        assert metadata["tone_policy"]["pattern_code"] == "D"
        assert metadata["tone_policy"]["tone_key"] == "warning_soft"
        assert metadata["tone_policy"]["policy_variant"] == "missed_dose.warning_soft.v1"
        assert metadata["adherence_pattern"]["message_validation"]["passed"] is True
        assert metadata["tone_policy"]["message_validation"]["passed"] is True
        assert metadata["llm_personalization"]["adjudication_source"] == "rule"
        assert metadata["llm_personalization"]["message_source"] == "csv_fallback"
        assert len(stubs) == 1


def test_ambiguous_pattern_can_use_llm_adjudication_and_generated_message():
    with build_session() as session:
        current = seed_slot_events(session, ["taken"] * 10 + ["missed"], medication_name="혈압약")[-1]
        create_missed_dose_conversation_alert(session, current, current.missed_detected_at)
        response = AgentResponse(
            trace_id="trace-hybrid-ambiguous",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={
                "missed_dose_hybrid": {
                    "reason": "공감형 톤에 맞춰 부담을 낮추는 표현을 생성했습니다.",
                    "pattern_code": "A",
                    "pattern_confidence": 0.82,
                    "judgement_reason": "최근 복용 흐름이 어느 정도 유지되어 단순 망각 가능성이 큽니다.",
                    "tone_key": "empathy",
                    "generated_message": "괜찮아요. 지금 상태를 알려주세요.",
                    "safety_notes": ["no_medication_name", "no_diagnosis", "non_directive"],
                }
            },
            human_summary="혈압약을 놓친 이유를 알려주세요.",
            requires_conversation_alert=True,
        )

        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=current.id)

        message = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one()
        metadata = json.loads(message.metadata_json)
        assert message.content == "괜찮아요. 지금 상태를 알려주세요."
        assert "공감형 톤에 맞춰" not in message.content
        assert metadata["adherence_pattern"]["pattern_code"] == "A"
        assert metadata["tone_policy"]["tone_key"] == "empathy"
        assert metadata["llm_personalization"]["rule_pattern_code"] == "B"
        assert metadata["llm_personalization"]["final_pattern_code"] == "A"
        assert metadata["llm_personalization"]["adjudication_source"] == "llm_adjudication"
        assert metadata["llm_personalization"]["message_source"] == "llm_generated"
        assert metadata["llm_personalization"]["llm_generation_reason"] == "공감형 톤에 맞춰 부담을 낮추는 표현을 생성했습니다."
        assert metadata["llm_personalization"]["llm_safety_notes"] == ["no_medication_name", "no_diagnosis", "non_directive"]
        assert metadata["adherence_pattern"]["message_validation"]["passed"] is True


def test_missed_dose_pro_card_keeps_validated_hybrid_message_as_chat_body():
    with build_session() as session:
        current = seed_slot_events(session, ["missed"], medication_name="\ud608\uc555\uc57d")[-1]
        create_missed_dose_conversation_alert(session, current, current.missed_detected_at)
        generated_message = "\uc810\uc2ec \uc2dc\uac04\uc5d0 \ub193\uce58\uc2e0 \uac83 \uac19\uc544\uc694. \ud3b8\ud558\uc2e4 \ub54c \ud655\uc778\ud574\ubcf4\uc138\uc694."
        response = AgentResponse(
            trace_id="trace-missed-dose-pro-card-body",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="side_effect_assessment",
            structured_payload={
                "missed_dose_hybrid": {
                    "reason": "routine_support",
                    "tone_key": "persuasion",
                    "generated_message": generated_message,
                    "safety_notes": ["no_medication_name", "no_diagnosis", "non_directive"],
                },
                "tool_results": [
                    {
                        "tool_name": "get_pro_ctcae_questionnaire",
                        "status": "success",
                        "response": {
                            "input_symptom": "\uba54\uc2a4\uaebc\uc6c0",
                            "matched": True,
                            "questions": [{"question": "\uc99d\uc0c1\uc774 \uc788\uc5c8\ub098\uc694?"}],
                        },
                    }
                ],
            },
            human_summary="```json\n{\"missed_dose_hybrid\": {}}\n```",
            requires_conversation_alert=True,
        )

        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=current.id)

        message = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one()
        metadata = json.loads(message.metadata_json)
        alert = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        assert message.content == generated_message
        assert alert.body == generated_message
        assert metadata["ae_pro_ctcae"]["input_symptom"] == "\uba54\uc2a4\uaebc\uc6c0"
        assert "```json" not in message.content


def test_clear_escalation_pattern_ignores_llm_downgrade_candidate():
    with build_session() as session:
        current = seed_slot_events(session, ["missed", "missed", "missed"], medication_name="혈압약")[-1]
        create_missed_dose_conversation_alert(session, current, current.missed_detected_at)
        response = AgentResponse(
            trace_id="trace-hybrid-clear-rule",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={
                "missed_dose_hybrid": {
                    "pattern_code": "B",
                    "pattern_confidence": 0.91,
                    "judgement_reason": "루틴 형성 문제로 보입니다.",
                    "tone_key": "persuasion",
                    "message": "혈압약은 반드시 복용하세요.",
                }
            },
            human_summary="혈압약을 놓친 이유를 알려주세요.",
            requires_conversation_alert=True,
        )

        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=current.id)

        message = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one()
        metadata = json.loads(message.metadata_json)
        assert message.content == PATTERN_MESSAGES["D"]
        assert "혈압약" not in message.content
        assert metadata["adherence_pattern"]["pattern_code"] == "D"
        assert metadata["llm_personalization"]["adjudication_source"] == "rule"
        assert metadata["llm_personalization"]["message_source"] == "csv_fallback"
        assert session.query(Notification).filter(Notification.notification_type == CLINICIAN_ESCALATION_TYPE).count() == 1


def test_llm_generated_message_falls_back_when_safety_validation_fails():
    with build_session() as session:
        current = seed_slot_events(session, ["missed"], medication_name="혈압약")[-1]
        create_missed_dose_conversation_alert(session, current, current.missed_detected_at)
        response = AgentResponse(
            trace_id="trace-hybrid-unsafe-message",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={
                "missed_dose_hybrid": {
                    "reason": "설득형 문구를 만들었지만 금지 표현이 포함되었습니다.",
                    "pattern_code": "B",
                    "pattern_confidence": 0.8,
                    "tone_key": "persuasion",
                    "generated_message": "혈압약은 반드시 복용하세요.",
                }
            },
            human_summary="혈압약을 놓친 이유를 알려주세요.",
            requires_conversation_alert=True,
        )

        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=current.id)

        message = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one()
        metadata = json.loads(message.metadata_json)
        assert message.content == PATTERN_MESSAGES["B"]
        assert metadata["llm_personalization"]["message_source"] == "csv_fallback"
        assert metadata["llm_personalization"]["llm_generation_reason"] == "설득형 문구를 만들었지만 금지 표현이 포함되었습니다."
        assert metadata["llm_personalization"]["fallback_reason"] == "llm_message_failed_safety_validation"
        assert "forbidden_term:혈압약" in metadata["llm_personalization"]["llm_message_validation"]["errors"]


def test_general_llm_message_fields_are_not_used_as_missed_dose_generated_message():
    with build_session() as session:
        current = seed_slot_events(session, ["missed"], medication_name="혈압약")[-1]
        create_missed_dose_conversation_alert(session, current, current.missed_detected_at)
        response = AgentResponse(
            trace_id="trace-hybrid-general-message-ignored",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={
                "missed_dose_hybrid": {
                    "pattern_code": "B",
                    "pattern_confidence": 0.8,
                    "tone_key": "persuasion",
                    "message": "혈압약은 반드시 복용하세요.",
                    "patient_message": "혈압약 복약을 확인해주세요.",
                },
                "model_output": {
                    "message": "혈압약 복약을 놓치셨습니다.",
                    "patient_message": "혈압약을 지금 확인해주세요.",
                },
            },
            human_summary="혈압약을 놓친 이유를 알려주세요.",
            requires_conversation_alert=True,
        )

        persist_agent_summary(session, response, category="missed_dose", related_dose_event_id=current.id)

        message = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").one()
        metadata = json.loads(message.metadata_json)
        assert message.content == PATTERN_MESSAGES["B"]
        assert metadata["llm_personalization"]["message_source"] == "csv_fallback"
        assert metadata["llm_personalization"]["fallback_reason"] == "llm_message_missing"
        assert metadata["llm_personalization"]["llm_message_candidate"] == ""
        assert metadata["llm_personalization"]["llm_message_validation"]["errors"] == ["message_empty"]


def add_persona_reaction_history(
    session,
    current: DoseEvent,
    *,
    tone_key: str,
    action: str = "ignore",
    success: bool = False,
    days_ago: int = 1,
    persona_id: str = "persona-test",
) -> ChatMessage:
    observed_at = current.scheduled_for - timedelta(days=days_ago)
    message = ChatMessage(
        patient_id=current.patient_id,
        role="assistant",
        sender_type="assistant",
        category="missed_dose",
        content="history",
        created_at=observed_at,
        metadata_json=json.dumps(
            {
                "tone_policy": {
                    "pattern_code": "B",
                    "tone_key": tone_key,
                    "policy_variant": f"missed_dose.{tone_key}.v1",
                    "intensity": 1,
                    "selection_reason": "history_fixture",
                    "slot_label": current.slot_label,
                },
                "persona_reaction": {
                    "persona_id": persona_id,
                    "action": action,
                    "success": success,
                    "tone_key": tone_key,
                    "policy_variant": f"missed_dose.{tone_key}.v1",
                    "observed_at": observed_at.isoformat(),
                    "simulation_day": days_ago,
                },
            },
            ensure_ascii=False,
        ),
    )
    session.add(message)
    session.flush()
    return message


def test_tone_policy_escalates_persuasion_after_two_failures():
    with build_session() as session:
        current = seed_slot_events(session, ["missed"])[-1]
        add_persona_reaction_history(session, current, tone_key="persuasion", days_ago=1)
        add_persona_reaction_history(session, current, tone_key="persuasion", days_ago=2)

        decision = select_tone_policy(session, current, "B")

        assert decision.tone_key == "practical"
        assert decision.selection_reason == "escalated_after_two_persuasion_failures"


def test_tone_policy_escalates_practical_after_two_practical_failures():
    with build_session() as session:
        current = seed_slot_events(session, ["missed"])[-1]
        add_persona_reaction_history(session, current, tone_key="persuasion", days_ago=1)
        add_persona_reaction_history(session, current, tone_key="persuasion", days_ago=2)
        add_persona_reaction_history(session, current, tone_key="practical", action="snooze", days_ago=3)
        add_persona_reaction_history(session, current, tone_key="practical", action="snooze", days_ago=4)

        decision = select_tone_policy(session, current, "B")

        assert decision.tone_key == "warning_soft"
        assert decision.selection_reason == "escalated_after_two_practical_failures"


def test_tone_policy_reuses_recent_take_success_tone():
    with build_session() as session:
        current = seed_slot_events(session, ["missed"])[-1]
        add_persona_reaction_history(session, current, tone_key="warning_soft", action="take", success=True)

        decision = select_tone_policy(session, current, "B")

        assert decision.tone_key == "warning_soft"
        assert decision.selection_reason == "reuse_recent_take_success_tone"


def test_tone_policy_rotates_csv_message_variant_for_recent_same_tone_message():
    with build_session() as session:
        current = seed_slot_events(session, ["missed"])[-1]

        first = select_tone_policy(session, current, "B")
        session.add(
            ChatMessage(
                patient_id=current.patient_id,
                role="assistant",
                sender_type="assistant",
                category="missed_dose",
                content=first.message,
                related_dose_event_id=current.id,
                metadata_json=json.dumps({"tone_policy": first.to_metadata()}, ensure_ascii=False),
            )
        )
        session.flush()

        second = select_tone_policy(session, current, "B")

        assert first.tone_key == "persuasion"
        assert first.message_variant == "v1"
        assert second.tone_key == "persuasion"
        assert second.message_variant == "v2"
        assert second.message != first.message


def test_tone_policy_rotates_csv_message_variant_across_same_day_slots():
    with build_session() as session:
        morning = seed_slot_events(session, ["missed"], slot_label="아침 08:00", scheduled_time="08:00")[-1]
        lunch = seed_slot_events(session, ["missed"], slot_label="점심 13:00", scheduled_time="13:00")[-1]

        first = select_tone_policy(session, morning, "B")
        session.add(
            ChatMessage(
                patient_id=morning.patient_id,
                role="assistant",
                sender_type="assistant",
                category="missed_dose",
                content=first.message,
                related_dose_event_id=morning.id,
                created_at=morning.missed_detected_at,
                metadata_json=json.dumps({"tone_policy": first.to_metadata()}, ensure_ascii=False),
            )
        )
        session.flush()

        second = select_tone_policy(session, lunch, "B")

        assert first.message_variant == "v1"
        assert second.message_variant == "v2"
        assert second.message != first.message
        assert second.slot_label == "점심 13:00"


def test_tone_policy_c_overrides_persona_history():
    with build_session() as session:
        current = seed_slot_events(session, ["missed", "missed", "missed"])[-1]
        add_persona_reaction_history(session, current, tone_key="warning_soft", action="take", success=True)
        create_notification(
            session,
            notification_type="conversation_alert",
            title="부작용 기록 후 알림 확인",
            body="알림을 유지합니다.",
            visible_at=current.missed_detected_at - timedelta(days=1),
            metadata={
                "category": SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
                "status": "reply_completed",
                "action": "keep",
            },
        )
        pattern = evaluate_adherence_pattern(session, current)

        decision = select_tone_policy(session, current, pattern.pattern_code)

        assert pattern.pattern_code == "C"
        assert decision.tone_key == "side_effect_check"
        assert decision.selection_reason == "pattern_c_side_effect_priority"


def test_tone_policy_uses_recent_side_effect_reply_signal_before_success_reuse():
    with build_session() as session:
        previous, current = seed_slot_events(session, ["missed", "missed"])
        add_persona_reaction_history(session, current, tone_key="warning_soft", action="take", success=True)
        create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="미복용 이유를 알려주세요.",
            visible_at=previous.missed_detected_at,
            related_dose_event_id=previous.id,
            metadata={
                "category": "missed_dose",
                "status": "reply_submitted",
                "patient_reply": "속이 메스꺼워서 못 먹었어요.",
                "missed_dose_reply_understanding": {
                    "reply_intent": "missed_reason",
                    "barrier_type": "side_effect_concern",
                    "reaction_action": "reply",
                    "confidence": 0.84,
                    "evidence": ["side_effect_keyword"],
                    "policy_signals": {
                        "prefer_tone": "side_effect_check",
                        "avoid_tones": ["warning_soft"],
                        "needs_side_effect_check": True,
                        "needs_support_first": True,
                        "suppress_candidate": False,
                    },
                },
            },
        )

        decision = select_tone_policy(session, current, "B")

        assert decision.tone_key == "side_effect_check"
        assert decision.selection_reason == "recent_reply_side_effect_signal"


def test_tone_policy_uses_recent_support_first_signal_to_avoid_warning():
    with build_session() as session:
        previous, current = seed_slot_events(session, ["missed", "missed"])
        create_notification(
            session,
            notification_type="conversation_alert",
            title="AI가 대화를 요청합니다.",
            body="미복용 이유를 알려주세요.",
            visible_at=previous.missed_detected_at,
            related_dose_event_id=previous.id,
            metadata={
                "category": "missed_dose",
                "status": "reply_submitted",
                "patient_reply": "알림이 계속 오니까 부담돼요.",
                "missed_dose_reply_understanding": {
                    "reply_intent": "missed_reason",
                    "barrier_type": "notification_burden",
                    "reaction_action": "reply",
                    "confidence": 0.74,
                    "evidence": ["notification_burden_keyword"],
                    "policy_signals": {
                        "prefer_tone": "empathy",
                        "avoid_tones": ["warning_soft"],
                        "needs_side_effect_check": False,
                        "needs_support_first": True,
                        "suppress_candidate": False,
                    },
                },
            },
        )

        decision = select_tone_policy(session, current, "D")

        assert decision.tone_key == "empathy"
        assert decision.selection_reason == "recent_reply_support_first_signal"
