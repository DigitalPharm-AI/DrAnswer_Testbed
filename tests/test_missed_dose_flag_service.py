from datetime import date, datetime

from system_app.models import DoseEvent, DoseSchedule, MedicationPlan
from system_app.services.missed_dose_flag_service import (
    activate_missed_dose_flag,
    active_missed_dose_flag_for_date,
    clear_missed_dose_flag_after_taken,
    is_active_missed_dose_flag_for_event,
)
from tests.helpers import build_session


def add_event(session, *, plan, schedule, scheduled_for, status="scheduled", missed_detected_at=None):
    event = DoseEvent(
        patient_id="demo-patient",
        plan_id=plan.id,
        schedule_id=schedule.id,
        medication_name=plan.medication_name,
        slot_label=schedule.slot_label,
        scheduled_for=scheduled_for,
        status=status,
        missed_detected_at=missed_detected_at,
        missed_handled=status == "missed",
    )
    session.add(event)
    session.flush()
    return event


def test_same_day_subsequent_taken_clears_day_missed_dose_flag():
    with build_session() as session:
        plan = MedicationPlan(
            patient_id="demo-patient",
            medication_name="검증약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            active=True,
        )
        session.add(plan)
        session.flush()
        morning = DoseSchedule(plan_id=plan.id, slot_label="아침 08:00", scheduled_time="08:00")
        lunch = DoseSchedule(plan_id=plan.id, slot_label="점심 12:00", scheduled_time="12:00")
        session.add_all([morning, lunch])
        session.flush()
        morning_event = add_event(
            session,
            plan=plan,
            schedule=morning,
            scheduled_for=datetime(2026, 4, 20, 8, 0),
            status="missed",
            missed_detected_at=datetime(2026, 4, 20, 9, 30),
        )
        lunch_event = add_event(
            session,
            plan=plan,
            schedule=lunch,
            scheduled_for=datetime(2026, 4, 20, 12, 0),
            status="taken",
        )

        flag = activate_missed_dose_flag(session, morning_event, activated_at=morning_event.missed_detected_at)
        cleared = clear_missed_dose_flag_after_taken(session, lunch_event, taken_at=datetime(2026, 4, 20, 12, 5))

        assert flag.id == cleared.id
        assert cleared.active is False
        assert cleared.clear_reason == "subsequent_same_day_taken"
        assert active_missed_dose_flag_for_date(session, date(2026, 4, 20)) is None
        assert is_active_missed_dose_flag_for_event(session, morning_event.id) is False


def test_repeated_next_day_morning_miss_reactivates_flag_as_new_day_context():
    with build_session() as session:
        plan = MedicationPlan(
            patient_id="demo-patient",
            medication_name="검증약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 21),
            active=True,
        )
        session.add(plan)
        session.flush()
        morning = DoseSchedule(plan_id=plan.id, slot_label="아침 08:00", scheduled_time="08:00")
        lunch = DoseSchedule(plan_id=plan.id, slot_label="점심 12:00", scheduled_time="12:00")
        session.add_all([morning, lunch])
        session.flush()
        day1_morning = add_event(
            session,
            plan=plan,
            schedule=morning,
            scheduled_for=datetime(2026, 4, 20, 8, 0),
            status="missed",
            missed_detected_at=datetime(2026, 4, 20, 9, 30),
        )
        day1_lunch = add_event(
            session,
            plan=plan,
            schedule=lunch,
            scheduled_for=datetime(2026, 4, 20, 12, 0),
            status="taken",
        )
        day2_morning = add_event(
            session,
            plan=plan,
            schedule=morning,
            scheduled_for=datetime(2026, 4, 21, 8, 0),
            status="missed",
            missed_detected_at=datetime(2026, 4, 21, 9, 30),
        )

        activate_missed_dose_flag(session, day1_morning, activated_at=day1_morning.missed_detected_at)
        clear_missed_dose_flag_after_taken(session, day1_lunch, taken_at=datetime(2026, 4, 20, 12, 5))
        day2_flag = activate_missed_dose_flag(session, day2_morning, activated_at=day2_morning.missed_detected_at)

        assert active_missed_dose_flag_for_date(session, date(2026, 4, 20)) is None
        assert day2_flag.active is True
        assert day2_flag.trigger_slot_label == "아침 08:00"
        assert is_active_missed_dose_flag_for_event(session, day2_morning.id) is True
