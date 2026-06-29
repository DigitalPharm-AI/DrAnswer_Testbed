from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from shared.json_utils import dump_json, parse_json_object
from shared.schemas import AgentResponse
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.db import SessionLocal
from system_app.models import AgentJob, ChatMessage, DoseEvent, DoseSchedule, MedicationPlan, MissedDoseFlag, Notification, SimulationClock, SimulationPatientProfile
from system_app.services.agent_jobs import create_agent_job, mark_agent_job_failed
from system_app.services.agent_response_service import persist_agent_summary
from system_app.services.clock_service import ensure_clock
from system_app.services.dose_event_service import collect_missed_dose_payloads, create_missed_dose_conversation_alert, mark_dose_taken
from system_app.services.medication_plan_service import reset_simulation_state
from system_app.services.side_effect_reminder_safety import (
    SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
    set_reminder_suppressed_after_side_effect,
)
from system_app.services.simulation_constants import PHR_SYNC_SYNCED

settings = get_settings()
BASE_TIME = datetime(2026, 5, 5, 8, 0, 0)
MISS_DETECTED_OFFSET = timedelta(minutes=90)


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def set_clock(session, current_time: datetime) -> None:
    clock = ensure_clock(session)
    clock.current_time = current_time
    clock.is_running = False
    clock.speed_multiplier = 0
    clock.last_processed_sim_time = current_time
    clock.last_tick_real_at = utc_now()


def create_plan(session, *, start_offset_days: int = 20, with_night: bool = False):
    plan = MedicationPlan(
        patient_id=settings.patient_id,
        medication_name="당뇨약",
        dosage="1정",
        instructions="playwright adherence pattern fixture",
        start_date=(BASE_TIME - timedelta(days=start_offset_days)).date(),
        end_date=(BASE_TIME + timedelta(days=30)).date(),
        active=True,
    )
    session.add(plan)
    session.flush()
    morning = DoseSchedule(plan_id=plan.id, slot_label="아침 08:00", scheduled_time="08:00")
    session.add(morning)
    night = None
    if with_night:
        night = DoseSchedule(plan_id=plan.id, slot_label="야간 21:00", scheduled_time="21:00")
        session.add(night)
    session.flush()
    return plan, morning, night


def create_current_flow_plan(session, *, start_offset_days: int = 5):
    plan = MedicationPlan(
        patient_id=settings.patient_id,
        medication_name="당뇨약",
        dosage="1정",
        instructions="playwright current scenario fixture",
        start_date=(BASE_TIME - timedelta(days=start_offset_days)).date(),
        end_date=(BASE_TIME + timedelta(days=30)).date(),
        active=True,
    )
    session.add(plan)
    session.flush()
    morning = DoseSchedule(plan_id=plan.id, slot_label="아침 08:00", scheduled_time="08:00")
    lunch = DoseSchedule(plan_id=plan.id, slot_label="점심 13:00", scheduled_time="13:00")
    night = DoseSchedule(plan_id=plan.id, slot_label="야간 21:00", scheduled_time="21:00")
    session.add_all([morning, lunch, night])
    session.flush()
    return plan, morning, lunch, night


def add_event(session, plan, schedule, day_offset: int, status: str, *, hour: int = 8) -> DoseEvent:
    scheduled_for = (BASE_TIME + timedelta(days=day_offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
    event = DoseEvent(
        patient_id=settings.patient_id,
        plan_id=plan.id,
        schedule_id=schedule.id,
        medication_name=plan.medication_name,
        slot_label=schedule.slot_label,
        scheduled_for=scheduled_for,
        taken_at=scheduled_for + timedelta(minutes=5) if status == "taken" else None,
        status=status,
        alerts_generated=status in {"taken", "missed"},
        missed_handled=status == "missed",
        missed_detected_at=scheduled_for + MISS_DETECTED_OFFSET if status == "missed" else None,
    )
    session.add(event)
    session.flush()
    return event


def agent_response(
    trace_id: str,
    *,
    structured_payload: dict | None = None,
    human_summary: str = "복약을 놓친 상황을 확인하고 싶어요. 현재 상태를 알려주세요.",
) -> AgentResponse:
    return AgentResponse(
        trace_id=trace_id,
        agent_name="missed_dose_coach",
        prompt_version_id="playwright-fixture",
        decision_type="missed_dose_assessment",
        structured_payload=structured_payload or {"fixture": "playwright_adherence_pattern_matrix"},
        human_summary=human_summary,
        requires_conversation_alert=True,
    )


def finalize_missed_dose(session, event: DoseEvent, scenario_id: str, response: AgentResponse | None = None) -> None:
    visible_at = event.missed_detected_at or event.scheduled_for + MISS_DETECTED_OFFSET
    set_clock(session, visible_at)
    create_missed_dose_conversation_alert(session, event, visible_at)
    persist_agent_summary(session, response or agent_response(f"playwright-{scenario_id.lower()}"), category="missed_dose", related_dose_event_id=event.id)
    session.commit()


def seed_side_effect_keep(session, visible_at: datetime) -> None:
    session.add(
        Notification(
            patient_id=settings.patient_id,
            notification_type="conversation_alert",
            title="부작용 기록 후 알림 확인",
            body="부작용 알림 유지 fixture",
            visible_at=visible_at,
            acknowledged=True,
            metadata_json=dump_json(
                {
                    "category": SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
                    "status": "reply_completed",
                    "action": "keep",
                    "patient_reply": "알림 유지하기",
                }
            ),
        )
    )
    session.flush()


def seed_pattern(session, scenario_id: str) -> dict:
    reset_simulation_state(session)
    scenario = scenario_id.upper()
    with_night = scenario == "SLOT_SCOPED"
    plan, morning, night = create_plan(session, start_offset_days=20, with_night=with_night)
    current_event: DoseEvent | None = None

    if scenario == "A":
        current_event = add_event(session, plan, morning, 0, "missed")
        for offset in range(-1, -15, -1):
            add_event(session, plan, morning, offset, "taken")
    elif scenario == "B":
        current_event = add_event(session, plan, morning, 0, "missed")
    elif scenario == "C":
        current_event = add_event(session, plan, morning, 0, "missed")
        add_event(session, plan, morning, -1, "missed")
        add_event(session, plan, morning, -2, "missed")
        seed_side_effect_keep(session, current_event.missed_detected_at - timedelta(hours=1))
    elif scenario == "D":
        current_event = add_event(session, plan, morning, 0, "missed")
        add_event(session, plan, morning, -1, "missed")
        add_event(session, plan, morning, -2, "missed")
    elif scenario == "E":
        current_event = add_event(session, plan, morning, 0, "missed")
        add_event(session, plan, morning, -1, "missed")
        add_event(session, plan, morning, -2, "taken")
        for offset in range(-3, -16, -1):
            add_event(session, plan, morning, offset, "missed")
    elif scenario == "SLOT_SCOPED":
        current_event = add_event(session, plan, morning, 0, "missed")
        add_event(session, plan, morning, -1, "missed")
        add_event(session, plan, morning, -2, "taken")
        add_event(session, plan, night, 0, "taken", hour=21)
        add_event(session, plan, night, -1, "taken", hour=21)
        add_event(session, plan, night, -2, "taken", hour=21)
    else:
        raise ValueError(f"unsupported_scenario:{scenario_id}")

    finalize_missed_dose(session, current_event, scenario)
    return inspect_state(session)


def seed_suppression(session) -> dict:
    reset_simulation_state(session)
    plan, morning, _ = create_plan(session, start_offset_days=1)
    event = add_event(session, plan, morning, 0, "scheduled")
    due_at = event.scheduled_for + MISS_DETECTED_OFFSET
    set_clock(session, due_at)
    set_reminder_suppressed_after_side_effect(session, True, reason="playwright suppression fixture")
    payloads = collect_missed_dose_payloads(session, due_at)
    session.commit()
    payload = inspect_state(session)
    payload["missed_payload_count"] = len(payloads)
    return payload


def latest_missed_event(session) -> DoseEvent:
    event = (
        session.query(DoseEvent)
        .filter(DoseEvent.status == "missed")
        .order_by(DoseEvent.scheduled_for.desc(), DoseEvent.id.desc())
        .first()
    )
    if event is None:
        raise ValueError("missed_event_not_found")
    return event


def seed_complex(session, scenario_id: str) -> dict:
    reset_simulation_state(session)
    scenario = scenario_id.upper()
    if scenario == "C_SUPPRESSION_AFTER_KEEP":
        return seed_complex_c_suppression_after_keep(session)
    if scenario == "D_ESCALATION_IDEMPOTENCY":
        return seed_complex_d_escalation_idempotency(session)
    if scenario == "E_REPLY_WITH_MIXED_SLOTS":
        return seed_complex_e_reply_with_mixed_slots(session)
    raise ValueError(f"unsupported_complex_scenario:{scenario_id}")


def seed_complex_c_suppression_after_keep(session) -> dict:
    plan, morning, night = create_plan(session, start_offset_days=20, with_night=True)
    current_event = add_event(session, plan, morning, 0, "missed")
    add_event(session, plan, morning, -1, "missed")
    add_event(session, plan, morning, -2, "missed")
    add_event(session, plan, night, -1, "taken", hour=21)
    add_event(session, plan, night, -2, "taken", hour=21)
    seed_side_effect_keep(session, current_event.missed_detected_at - timedelta(hours=2))
    finalize_missed_dose(session, current_event, "complex-c-suppression-after-keep")
    payload = inspect_state(session)
    payload["phase"] = "initial_c_priority"
    return payload


def apply_suppression_and_trigger_next_missed(session) -> dict:
    event = latest_missed_event(session)
    plan = session.get(MedicationPlan, event.plan_id)
    schedule = session.get(DoseSchedule, event.schedule_id)
    if plan is None or schedule is None:
        raise ValueError("latest_missed_event_has_no_plan_or_schedule")
    set_reminder_suppressed_after_side_effect(session, True, reason="playwright complex scenario suppression")
    next_event = add_event(session, plan, schedule, 1, "scheduled")
    due_at = next_event.scheduled_for + MISS_DETECTED_OFFSET
    set_clock(session, due_at)
    before_alert_count = session.query(Notification).filter(Notification.notification_type == "conversation_alert").count()
    before_message_count = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").count()
    payloads = collect_missed_dose_payloads(session, due_at)
    session.commit()
    payload = inspect_state(session)
    payload.update(
        {
            "phase": "after_suppressed_followup",
            "missed_payload_count": len(payloads),
            "conversation_alert_count_before_followup": before_alert_count,
            "missed_dose_message_count_before_followup": before_message_count,
            "next_event_status": session.get(DoseEvent, next_event.id).status,
        }
    )
    return payload


def seed_complex_d_escalation_idempotency(session) -> dict:
    plan, morning, _ = create_plan(session, start_offset_days=20)
    current_event = add_event(session, plan, morning, 0, "missed")
    add_event(session, plan, morning, -1, "missed")
    add_event(session, plan, morning, -2, "missed")
    finalize_missed_dose(session, current_event, "complex-d-escalation-idempotency")
    payload = inspect_state(session)
    payload["phase"] = "initial_d_escalation"
    return payload


def replay_latest_missed_dose(session) -> dict:
    event = latest_missed_event(session)
    before_escalations = session.query(Notification).filter(Notification.notification_type == "clinician_escalation").count()
    before_messages = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").count()
    persist_agent_summary(
        session,
        agent_response(f"playwright-replay-{event.id}"),
        category="missed_dose",
        related_dose_event_id=event.id,
    )
    session.commit()
    payload = inspect_state(session)
    payload.update(
        {
            "phase": "after_replay",
            "clinician_escalation_count_before_replay": before_escalations,
            "missed_dose_message_count_before_replay": before_messages,
        }
    )
    return payload


def seed_complex_e_reply_with_mixed_slots(session) -> dict:
    plan, morning, night = create_plan(session, start_offset_days=20, with_night=True)
    current_event = add_event(session, plan, morning, 0, "missed")
    add_event(session, plan, morning, -1, "missed")
    add_event(session, plan, morning, -2, "taken")
    for offset in range(-3, -16, -1):
        add_event(session, plan, morning, offset, "missed")
    for offset in range(0, -6, -1):
        add_event(session, plan, night, offset, "taken", hour=21)
    finalize_missed_dose(session, current_event, "complex-e-reply-with-mixed-slots")
    payload = inspect_state(session)
    payload["phase"] = "initial_e_with_mixed_slots"
    return payload


def seed_current_flag_flow(session) -> dict:
    reset_simulation_state(session)
    plan, morning, lunch, night = create_current_flow_plan(session, start_offset_days=5)
    morning_event = add_event(session, plan, morning, 0, "missed", hour=8)
    lunch_event = add_event(session, plan, lunch, 0, "scheduled", hour=13)
    night_event = add_event(session, plan, night, 0, "scheduled", hour=21)
    finalize_missed_dose(session, morning_event, "current-flag-flow")
    payload = inspect_state(session)
    payload["phase"] = "current_flag_active_after_morning_missed"
    payload["current_flow"] = {
        "morning_event_id": morning_event.id,
        "lunch_event_id": lunch_event.id,
        "night_event_id": night_event.id,
    }
    return payload


def seed_current_ready_clock(session) -> dict:
    reset_simulation_state(session)
    create_current_flow_plan(session, start_offset_days=1)
    profile = SimulationPatientProfile(
        local_patient_id=settings.patient_id,
        phr_patient_key="playwright-phr-key",
        sync_status=PHR_SYNC_SYNCED,
        error_message="",
        registered_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(profile)
    session.commit()
    payload = inspect_state(session)
    payload["phase"] = "current_ready_clock"
    return payload


def seed_current_repeat_morning_flow(session) -> dict:
    reset_simulation_state(session)
    plan, morning, lunch, night = create_current_flow_plan(session, start_offset_days=5)
    day1_morning = add_event(session, plan, morning, 0, "missed", hour=8)
    day1_lunch = add_event(session, plan, lunch, 0, "scheduled", hour=13)
    day1_night = add_event(session, plan, night, 0, "taken", hour=21)
    finalize_missed_dose(session, day1_morning, "current-repeat-day1")
    mark_dose_taken(session, day1_lunch.id, taken_at=day1_lunch.scheduled_for + timedelta(minutes=5))

    day2_morning = add_event(session, plan, morning, 1, "missed", hour=8)
    add_event(session, plan, lunch, 1, "scheduled", hour=13)
    add_event(session, plan, night, 1, "scheduled", hour=21)
    finalize_missed_dose(session, day2_morning, "current-repeat-day2")
    payload = inspect_state(session)
    payload["phase"] = "current_repeat_morning_separate_problem"
    payload["current_flow"] = {
        "day1_morning_event_id": day1_morning.id,
        "day1_lunch_event_id": day1_lunch.id,
        "day1_night_event_id": day1_night.id,
        "day2_morning_event_id": day2_morning.id,
    }
    return payload


def seed_hard_llm_safety_fallback(session) -> dict:
    reset_simulation_state(session)
    plan, morning, _ = create_plan(session, start_offset_days=5)
    current_event = add_event(session, plan, morning, 0, "missed")
    unsafe_response = agent_response(
        "playwright-hard-llm-safety-fallback",
        structured_payload={
            "missed_dose_hybrid": {
                "tone_key": "persuasion",
                "generated_message": "당뇨약을 놓치면 위험해요. 지금 복용하세요.",
            }
        },
        human_summary="당뇨약을 놓치면 위험해요. 지금 복용하세요.",
    )
    finalize_missed_dose(session, current_event, "hard-llm-safety-fallback", response=unsafe_response)
    payload = inspect_state(session)
    payload["phase"] = "hard_llm_safety_fallback"
    return payload


def seed_hard_agent_failure_no_stuck(session) -> dict:
    reset_simulation_state(session)
    plan, morning, _ = create_plan(session, start_offset_days=1)
    event = add_event(session, plan, morning, 0, "scheduled")
    due_at = event.scheduled_for + MISS_DETECTED_OFFSET
    set_clock(session, due_at)
    payloads = collect_missed_dose_payloads(session, due_at)
    if not payloads:
        raise ValueError("missed_dose_payload_not_created")
    payload = payloads[0]
    job = create_agent_job(session, "missed_dose", payload)
    mark_agent_job_failed(session, job.id, "playwright forced agent timeout")

    alert = (
        session.query(Notification)
        .filter(
            Notification.notification_type == "conversation_alert",
            Notification.related_dose_event_id == payload.dose_event_id,
            Notification.acknowledged.is_(False),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .first()
    )
    if alert is None:
        raise ValueError("awaiting_agent_alert_not_found")
    metadata = parse_json_object(alert.metadata_json)
    metadata.update(
        {
            "status": "agent_error",
            "agent_job_id": job.id,
            "error_type": "agent_timeout",
            "trace_id": "playwright-hard-agent-timeout",
        }
    )
    alert.body = "AI가 미복용 상황을 처리하지 못했습니다. AI 에이전트 오류 알림에서 다시 시도할 수 있습니다."
    alert.metadata_json = dump_json(metadata)
    session.add(
        Notification(
            patient_id=settings.patient_id,
            notification_type="agent_error",
            title="AI 에이전트 오류",
            body="백그라운드 AI 작업을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.",
            visible_at=due_at,
            related_dose_event_id=payload.dose_event_id,
            metadata_json=dump_json(
                {
                    "source_event_type": "missed_dose",
                    "error_type": "agent_timeout",
                    "trace_id": "playwright-hard-agent-timeout",
                    "decision_type": "missed_dose_assessment",
                    "agent_job_id": job.id,
                }
            ),
        )
    )
    session.commit()
    result = inspect_state(session)
    result["phase"] = "hard_agent_failure_no_stuck"
    return result


def clear_persona_cycle_runtime(session) -> None:
    for message in session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").all():
        message.related_dose_event_id = None
    for notification in session.query(Notification).all():
        notification.related_dose_event_id = None
        notification.acknowledged = True
    session.flush()
    session.execute(delete(MissedDoseFlag))
    session.execute(delete(DoseEvent))
    session.execute(delete(DoseSchedule))
    session.execute(delete(MedicationPlan))
    session.flush()


def seed_persona_round(session, *, pattern_code: str, simulation_day: int) -> dict:
    clear_persona_cycle_runtime(session)
    scenario = pattern_code.upper()
    plan, morning, night = create_plan(session, start_offset_days=20, with_night=scenario == "SLOT_SCOPED")
    current_event: DoseEvent | None = None
    if scenario == "A":
        current_event = add_event(session, plan, morning, 0, "missed")
        for offset in range(-1, -15, -1):
            add_event(session, plan, morning, offset, "taken")
    elif scenario == "B":
        current_event = add_event(session, plan, morning, 0, "missed")
    elif scenario == "C":
        current_event = add_event(session, plan, morning, 0, "missed")
        add_event(session, plan, morning, -1, "missed")
        add_event(session, plan, morning, -2, "missed")
        seed_side_effect_keep(session, current_event.missed_detected_at - timedelta(hours=1))
    elif scenario == "D":
        current_event = add_event(session, plan, morning, 0, "missed")
        add_event(session, plan, morning, -1, "missed")
        add_event(session, plan, morning, -2, "missed")
    elif scenario == "E":
        current_event = add_event(session, plan, morning, 0, "missed")
        add_event(session, plan, morning, -1, "missed")
        add_event(session, plan, morning, -2, "taken")
        for offset in range(-3, -16, -1):
            add_event(session, plan, morning, offset, "missed")
    else:
        raise ValueError(f"unsupported_persona_round_pattern:{pattern_code}")
    finalize_missed_dose(session, current_event, f"persona-{scenario}-{simulation_day}")
    payload = inspect_state(session)
    payload["phase"] = "persona_round_seeded"
    payload["requested_pattern_code"] = scenario
    payload["simulation_day"] = simulation_day
    return payload


def latest_missed_dose_message(session) -> ChatMessage:
    message = (
        session.query(ChatMessage)
        .filter(ChatMessage.category == "missed_dose")
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .first()
    )
    if message is None:
        raise ValueError("missed_dose_message_not_found")
    return message


def record_persona_reaction(
    session,
    *,
    persona_id: str,
    action: str,
    success: bool,
    simulation_day: int,
) -> dict:
    message = latest_missed_dose_message(session)
    metadata = parse_json_object(message.metadata_json)
    tone_policy = metadata.get("tone_policy") if isinstance(metadata.get("tone_policy"), dict) else {}
    observed_at = ensure_clock(session).current_time
    metadata["persona_reaction"] = {
        "persona_id": persona_id,
        "action": action,
        "success": success,
        "tone_key": tone_policy.get("tone_key", ""),
        "policy_variant": tone_policy.get("policy_variant", ""),
        "observed_at": observed_at.isoformat(),
        "simulation_day": simulation_day,
    }
    message.metadata_json = dump_json(metadata)
    if action == "take" and message.related_dose_event_id:
        mark_dose_taken(session, message.related_dose_event_id, taken_at=observed_at)
    elif action in {"reply", "distress_reply"}:
        session.add(
            ChatMessage(
                patient_id=settings.patient_id,
                role="user",
                sender_type="patient",
                category="multiturn_chat",
                content="요즘 알림에 어떻게 반응해야 할지 고민돼요.",
                created_at=observed_at,
                metadata_json=dump_json({"persona_id": persona_id, "simulation_day": simulation_day}),
            )
        )
        if action == "distress_reply":
            set_reminder_suppressed_after_side_effect(session, True, reason="playwright persona distress reaction")
    elif action == "suppress":
        set_reminder_suppressed_after_side_effect(session, True, reason="playwright persona suppress reaction")
    session.commit()
    payload = inspect_state(session)
    payload["phase"] = "persona_reaction_recorded"
    payload["recorded_reaction"] = metadata["persona_reaction"]
    return payload


def trigger_persona_suppressed_followup(session) -> dict:
    before_alert_count = session.query(Notification).filter(Notification.notification_type == "conversation_alert").count()
    before_message_count = session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").count()
    clear_persona_cycle_runtime(session)
    plan, morning, _ = create_plan(session, start_offset_days=1)
    event = add_event(session, plan, morning, 1, "scheduled")
    due_at = event.scheduled_for + MISS_DETECTED_OFFSET
    set_clock(session, due_at)
    payloads = collect_missed_dose_payloads(session, due_at)
    session.commit()
    payload = inspect_state(session)
    payload.update(
        {
            "phase": "persona_suppressed_followup",
            "missed_payload_count": len(payloads),
            "conversation_alert_count_before_followup": before_alert_count,
            "missed_dose_message_count_before_followup": before_message_count,
            "next_event_status": session.get(DoseEvent, event.id).status,
        }
    )
    return payload


def inspect_state(session) -> dict:
    message = (
        session.query(ChatMessage)
        .filter(ChatMessage.category == "missed_dose")
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .first()
    )
    latest_user_message = (
        session.query(ChatMessage)
        .filter(ChatMessage.role == "user")
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .first()
    )
    latest_system_request = (
        session.query(Notification)
        .filter(Notification.notification_type == "system_policy_request")
        .order_by(Notification.visible_at.desc(), Notification.id.desc())
        .first()
    )
    message_metadata = parse_json_object(message.metadata_json) if message is not None else {}
    latest_user_metadata = parse_json_object(latest_user_message.metadata_json) if latest_user_message is not None else {}
    latest_system_request_metadata = parse_json_object(latest_system_request.metadata_json) if latest_system_request is not None else {}
    pattern = message_metadata.get("adherence_pattern") if isinstance(message_metadata.get("adherence_pattern"), dict) else {}
    tone_policy = message_metadata.get("tone_policy") if isinstance(message_metadata.get("tone_policy"), dict) else {}
    llm_personalization = (
        message_metadata.get("llm_personalization") if isinstance(message_metadata.get("llm_personalization"), dict) else {}
    )
    persona_reaction = message_metadata.get("persona_reaction") if isinstance(message_metadata.get("persona_reaction"), dict) else {}
    clinician_rows = session.query(Notification).filter(Notification.notification_type == "clinician_escalation").all()
    agent_error_rows = session.query(Notification).filter(Notification.notification_type == "agent_error").order_by(Notification.visible_at.desc(), Notification.id.desc()).all()
    conversation_rows = session.query(Notification).filter(Notification.notification_type == "conversation_alert").all()
    flags = session.query(MissedDoseFlag).order_by(MissedDoseFlag.flag_date.asc(), MissedDoseFlag.id.asc()).all()
    agent_jobs = session.query(AgentJob).order_by(AgentJob.id.asc()).all()
    clock = ensure_clock(session)
    unack_missed_alerts = []
    for row in conversation_rows:
        metadata = parse_json_object(row.metadata_json)
        if metadata.get("category") == "missed_dose" and not row.acknowledged:
            unack_missed_alerts.append(row)
    return {
        "chat_content": message.content if message is not None else "",
        "message_created_at": message.created_at.isoformat() if message is not None else "",
        "clock_current_time": clock.current_time.isoformat(),
        "message_count": session.query(ChatMessage).count(),
        "missed_dose_message_count": session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").count(),
        "conversation_alert_count": len(conversation_rows),
        "unack_missed_conversation_alert_count": len(unack_missed_alerts),
        "clinician_escalation_count": len(clinician_rows),
        "clinician_escalation_metadata": [parse_json_object(row.metadata_json) for row in clinician_rows],
        "agent_error_count": len(agent_error_rows),
        "latest_agent_error_body": agent_error_rows[0].body if agent_error_rows else "",
        "latest_agent_error_metadata": parse_json_object(agent_error_rows[0].metadata_json) if agent_error_rows else {},
        "agent_jobs": [
            {
                "id": job.id,
                "job_type": job.job_type,
                "status": job.status,
                "attempts": job.attempts,
                "related_dose_event_id": job.related_dose_event_id,
                "error_message": job.error_message,
            }
            for job in agent_jobs
        ],
        "missed_dose_flags": [
            {
                "id": flag.id,
                "flag_date": flag.flag_date.isoformat(),
                "active": flag.active,
                "trigger_slot_label": flag.trigger_slot_label,
                "related_dose_event_id": flag.related_dose_event_id,
                "activated_at": flag.activated_at.isoformat() if flag.activated_at else "",
                "cleared_at": flag.cleared_at.isoformat() if flag.cleared_at else "",
                "clear_reason": flag.clear_reason,
                "subsequent_taken_count": flag.subsequent_taken_count,
            }
            for flag in flags
        ],
        "pattern_code": pattern.get("pattern_code", ""),
        "pattern_label": pattern.get("pattern_label", ""),
        "latest_adherence_pattern": pattern,
        "tone_policy": tone_policy,
        "latest_llm_personalization": llm_personalization,
        "tone_key": tone_policy.get("tone_key", ""),
        "policy_variant": tone_policy.get("policy_variant", ""),
        "persona_reaction": persona_reaction,
        "related_dose_event_id": message.related_dose_event_id if message is not None else None,
        "latest_user_message": latest_user_message.content if latest_user_message is not None else "",
        "latest_user_metadata": latest_user_metadata,
        "latest_system_request_metadata": latest_system_request_metadata,
        "pattern_message_validation": pattern.get("message_validation", {}),
        "streak_metrics": pattern.get("streak_metrics", {}),
    }


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "success"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "seed",
            "seed-suppression",
            "seed-complex",
            "suppress-next-missed",
            "replay-latest-missed",
            "persona-reset",
            "persona-seed-round",
            "persona-record-reaction",
            "persona-suppressed-followup",
            "seed-current-flag-flow",
            "seed-current-ready-clock",
            "seed-current-repeat-morning-flow",
            "seed-hard-llm-safety-fallback",
            "seed-hard-agent-failure-no-stuck",
            "inspect",
        ],
    )
    parser.add_argument("scenario", nargs="?")
    parser.add_argument("--persona-id", default="persona")
    parser.add_argument("--action", default="ignore")
    parser.add_argument("--success", default="false")
    parser.add_argument("--simulation-day", type=int, default=1)
    parser.add_argument("--pattern", default="B")
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.command == "seed":
            if not args.scenario:
                raise SystemExit("scenario is required")
            print_json(seed_pattern(session, args.scenario))
        elif args.command == "seed-suppression":
            print_json(seed_suppression(session))
        elif args.command == "seed-complex":
            if not args.scenario:
                raise SystemExit("scenario is required")
            print_json(seed_complex(session, args.scenario))
        elif args.command == "suppress-next-missed":
            print_json(apply_suppression_and_trigger_next_missed(session))
        elif args.command == "replay-latest-missed":
            print_json(replay_latest_missed_dose(session))
        elif args.command == "persona-reset":
            reset_simulation_state(session)
            print_json({"status": "reset"})
        elif args.command == "persona-seed-round":
            print_json(seed_persona_round(session, pattern_code=args.pattern, simulation_day=args.simulation_day))
        elif args.command == "persona-record-reaction":
            print_json(
                record_persona_reaction(
                    session,
                    persona_id=args.persona_id,
                    action=args.action,
                    success=parse_bool(args.success),
                    simulation_day=args.simulation_day,
                )
            )
        elif args.command == "persona-suppressed-followup":
            print_json(trigger_persona_suppressed_followup(session))
        elif args.command == "seed-current-flag-flow":
            print_json(seed_current_flag_flow(session))
        elif args.command == "seed-current-ready-clock":
            print_json(seed_current_ready_clock(session))
        elif args.command == "seed-current-repeat-morning-flow":
            print_json(seed_current_repeat_morning_flow(session))
        elif args.command == "seed-hard-llm-safety-fallback":
            print_json(seed_hard_llm_safety_fallback(session))
        elif args.command == "seed-hard-agent-failure-no-stuck":
            print_json(seed_hard_agent_failure_no_stuck(session))
        else:
            print_json(inspect_state(session))


if __name__ == "__main__":
    main()
