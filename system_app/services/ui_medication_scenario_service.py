from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import (
    AgentJob,
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    MissedDoseFlag,
    Notification,
    SideEffectRecord,
    TestMedicationScenario,
    TestMedicationScenarioItem,
)
from system_app.services.clock_service import ensure_clock
from system_app.services.dose_event_service import ensure_day_events
from system_app.services.nutrition_service import ensure_nutrition_profile
from system_app.services.ui_time import as_seoul_iso

TESTBED_SCENARIO_SOURCE = "testbed_scenario"
TESTBED_SCENARIO_END_DATE = date(9999, 12, 31)

_MEDICATIONS: dict[str, dict[str, str]] = {
    "med-metformin-500": {
        "medication_name": "메트포르민",
        "dosage": "500mg",
        "treatment_area": "당뇨약",
        "slot_label": "아침",
        "scheduled_time": "08:00",
    },
    "med-amlodipine-5": {
        "medication_name": "암로디핀",
        "dosage": "5mg",
        "treatment_area": "고혈압약",
        "slot_label": "아침",
        "scheduled_time": "09:00",
    },
    "med-sunitinib-50": {
        "medication_name": "수니티닙",
        "dosage": "50mg",
        "treatment_area": "신장암 치료약",
        "slot_label": "점심",
        "scheduled_time": "13:00",
    },
    "med-letrozole-2-5": {
        "medication_name": "레트로졸",
        "dosage": "2.5mg",
        "treatment_area": "유방암 치료약",
        "slot_label": "저녁",
        "scheduled_time": "18:00",
    },
}

_SCENARIOS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("tb-diabetes-v1", "당뇨 관리", ("med-metformin-500",)),
    ("tb-hypertension-v1", "고혈압 관리", ("med-amlodipine-5",)),
    ("tb-kidney-cancer-v1", "신장암 치료", ("med-sunitinib-50",)),
    ("tb-breast-cancer-v1", "유방암 치료", ("med-letrozole-2-5",)),
    (
        "tb-combined-v1",
        "복합 복약",
        (
            "med-metformin-500",
            "med-amlodipine-5",
            "med-sunitinib-50",
            "med-letrozole-2-5",
        ),
    ),
)


def seed_test_medication_scenarios(session: Session) -> None:
    """Seed the selectable fixtures into Backend DB tables.

    The rows, rather than this module's constants, are the runtime source used
    by list/apply operations. Constants only define the deterministic seed.
    """

    for scenario_order, (scenario_id, name, medication_ids) in enumerate(_SCENARIOS):
        scenario = session.get(TestMedicationScenario, scenario_id)
        if scenario is None:
            scenario = TestMedicationScenario(
                scenario_id=scenario_id,
                name=name,
                active=True,
                sort_order=scenario_order,
            )
            session.add(scenario)
        else:
            scenario.name = name
            scenario.active = True
            scenario.sort_order = scenario_order
            scenario.updated_at = utc_now()
        session.flush()

        existing_items = {
            item.medication_id: item
            for item in session.scalars(
                select(TestMedicationScenarioItem).where(
                    TestMedicationScenarioItem.scenario_id == scenario_id
                )
            ).all()
        }
        expected_ids = set(medication_ids)
        for stale_id, stale_item in existing_items.items():
            if stale_id not in expected_ids:
                session.delete(stale_item)
        for item_order, medication_id in enumerate(medication_ids):
            fixture = _MEDICATIONS[medication_id]
            item = existing_items.get(medication_id)
            if item is None:
                item = TestMedicationScenarioItem(
                    scenario_id=scenario_id,
                    medication_id=medication_id,
                    **fixture,
                )
                session.add(item)
            else:
                for key, value in fixture.items():
                    setattr(item, key, value)
            item.sort_order = item_order
    session.flush()


def list_test_medication_scenarios(session: Session) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(TestMedicationScenario)
            .where(TestMedicationScenario.active.is_(True))
            .order_by(TestMedicationScenario.sort_order, TestMedicationScenario.scenario_id)
        ).all()
    )
    return [test_medication_scenario_view(session, row) for row in rows]


def test_medication_scenario_view(
    session: Session,
    scenario: TestMedicationScenario,
) -> dict[str, Any]:
    items = list(
        session.scalars(
            select(TestMedicationScenarioItem)
            .where(TestMedicationScenarioItem.scenario_id == scenario.scenario_id)
            .order_by(
                TestMedicationScenarioItem.sort_order,
                TestMedicationScenarioItem.id,
            )
        ).all()
    )
    return {
        "scenario_id": scenario.scenario_id,
        "name": scenario.name,
        "medications": [_scenario_item_view(item) for item in items],
    }


def get_test_medication_scenario(
    session: Session,
    scenario_id: str,
) -> TestMedicationScenario | None:
    return session.scalar(
        select(TestMedicationScenario).where(
            TestMedicationScenario.scenario_id == scenario_id,
            TestMedicationScenario.active.is_(True),
        )
    )


def apply_test_medication_scenario(
    session: Session,
    *,
    scenario_id: str,
    schedule_date: date,
) -> dict[str, Any]:
    settings = get_settings()
    scenario = get_test_medication_scenario(session, scenario_id)
    if scenario is None:
        raise ValueError("medication_scenario_not_found")
    items = list(
        session.scalars(
            select(TestMedicationScenarioItem)
            .where(TestMedicationScenarioItem.scenario_id == scenario_id)
            .order_by(
                TestMedicationScenarioItem.sort_order,
                TestMedicationScenarioItem.id,
            )
        ).all()
    )
    if not items:
        raise ValueError("medication_scenario_empty")

    ensure_nutrition_profile(
        session,
        patient_id=settings.patient_id,
        persist=True,
    )
    _replace_owned_scenario_plans_from_date(
        session,
        patient_id=settings.patient_id,
        schedule_date=schedule_date,
    )

    for item in items:
        plan = MedicationPlan(
            patient_id=settings.patient_id,
            medication_name=f"{item.medication_name} {item.dosage}".strip(),
            dosage=item.dosage,
            instructions="테스트베드 시나리오 일정",
            treatment_area=item.treatment_area,
            source_type=TESTBED_SCENARIO_SOURCE,
            source_key=scenario.scenario_id,
            start_date=schedule_date,
            end_date=TESTBED_SCENARIO_END_DATE,
            active=True,
        )
        session.add(plan)
        session.flush()
        session.add(
            DoseSchedule(
                plan_id=plan.id,
                slot_label=item.slot_label,
                scheduled_time=item.scheduled_time,
            )
        )
    session.flush()
    ensure_day_events(session, schedule_date)
    session.flush()
    return {
        "scenario": {
            "scenario_id": scenario.scenario_id,
            "name": scenario.name,
            "schedule_date": schedule_date.isoformat(),
        },
        "medications": medication_events_for_date(
            session,
            schedule_date,
            patient_id=settings.patient_id,
        ),
    }


def ensure_initial_testbed_scenario_state(session: Session) -> bool:
    """Ensure the current testbed day has one non-overlapping scenario."""

    settings = get_settings()
    ensure_nutrition_profile(
        session,
        patient_id=settings.patient_id,
        persist=True,
    )
    clock = ensure_clock(session)
    schedule_date = clock.current_time.date()

    current_testbed_plan_id = session.scalar(
        select(MedicationPlan.id)
        .where(
            MedicationPlan.patient_id == settings.patient_id,
            MedicationPlan.active.is_(True),
            MedicationPlan.source_type == TESTBED_SCENARIO_SOURCE,
            MedicationPlan.start_date <= schedule_date,
            MedicationPlan.end_date >= schedule_date,
        )
        .limit(1)
    )
    if current_testbed_plan_id is not None:
        ensure_day_events(session, schedule_date)
        return False

    future_testbed_plan_id = session.scalar(
        select(MedicationPlan.id)
        .where(
            MedicationPlan.patient_id == settings.patient_id,
            MedicationPlan.active.is_(True),
            MedicationPlan.source_type == TESTBED_SCENARIO_SOURCE,
            MedicationPlan.start_date > schedule_date,
        )
        .limit(1)
    )
    if future_testbed_plan_id is not None:
        return False

    existing_plan_id = session.scalar(
        select(MedicationPlan.id)
        .where(
            MedicationPlan.patient_id == settings.patient_id,
            MedicationPlan.active.is_(True),
            MedicationPlan.start_date <= schedule_date,
            MedicationPlan.end_date >= schedule_date,
        )
        .limit(1)
    )
    if existing_plan_id is not None:
        return False

    apply_test_medication_scenario(
        session,
        scenario_id="tb-combined-v1",
        schedule_date=schedule_date,
    )
    first_event = session.scalar(
        select(DoseEvent)
        .join(MedicationPlan, MedicationPlan.id == DoseEvent.plan_id)
        .where(
            DoseEvent.patient_id == settings.patient_id,
            MedicationPlan.source_type == TESTBED_SCENARIO_SOURCE,
            MedicationPlan.source_key == "tb-combined-v1",
            DoseEvent.scheduled_for >= datetime.combine(schedule_date, time.min),
            DoseEvent.scheduled_for <= datetime.combine(schedule_date, time.max),
        )
        .order_by(DoseEvent.scheduled_for, DoseEvent.id)
    )
    if first_event is not None:
        first_event.status = "taken"
        first_event.taken_at = first_event.scheduled_for
    clock.is_running = True
    clock.speed_multiplier = 60
    clock.last_tick_real_at = utc_now()
    session.flush()
    return True


def medication_events_for_date(
    session: Session,
    schedule_date: date,
    *,
    patient_id: str | None = None,
) -> list[dict[str, Any]]:
    target_patient_id = patient_id or get_settings().patient_id
    rows = list(
        session.execute(
            select(DoseEvent, MedicationPlan.treatment_area)
            .join(MedicationPlan, MedicationPlan.id == DoseEvent.plan_id)
            .where(
                DoseEvent.patient_id == target_patient_id,
                DoseEvent.scheduled_for >= datetime.combine(schedule_date, time.min),
                DoseEvent.scheduled_for <= datetime.combine(schedule_date, time.max),
            )
            .order_by(
                DoseEvent.scheduled_for,
                DoseEvent.medication_name,
                DoseEvent.id,
            )
        ).all()
    )
    return [
        dose_event_view(event, treatment_area=treatment_area)
        for event, treatment_area in rows
    ]


def dose_event_view(
    event: DoseEvent,
    *,
    treatment_area: str | None = None,
) -> dict[str, Any]:
    return {
        "dose_event_id": event.public_id,
        "medication_name": event.medication_name,
        "treatment_area": treatment_area or "",
        "slot_label": event.slot_label,
        "scheduled_for": as_seoul_iso(event.scheduled_for),
        "status": event.status,
        "taken_at": as_seoul_iso(event.taken_at) if event.taken_at else None,
    }


def active_test_medication_scenario(
    session: Session,
    schedule_date: date,
    *,
    patient_id: str | None = None,
) -> dict[str, Any] | None:
    target_patient_id = patient_id or get_settings().patient_id
    plan = session.scalar(
        select(MedicationPlan)
        .where(
            MedicationPlan.patient_id == target_patient_id,
            MedicationPlan.source_type == TESTBED_SCENARIO_SOURCE,
            MedicationPlan.active.is_(True),
            MedicationPlan.start_date <= schedule_date,
            MedicationPlan.end_date >= schedule_date,
        )
        .order_by(MedicationPlan.id)
    )
    if plan is None:
        return None
    scenario = session.get(TestMedicationScenario, plan.source_key)
    return {
        "scenario_id": plan.source_key,
        "name": scenario.name if scenario is not None else plan.source_key,
        "schedule_date": schedule_date.isoformat(),
    }


def _scenario_item_view(item: TestMedicationScenarioItem) -> dict[str, Any]:
    return {
        "medication_id": item.medication_id,
        "medication_name": item.medication_name,
        "dosage": item.dosage,
        "treatment_area": item.treatment_area,
        "slot_label": item.slot_label,
        "scheduled_time": item.scheduled_time,
    }


def _replace_owned_scenario_plans_from_date(
    session: Session,
    *,
    patient_id: str,
    schedule_date: date,
) -> None:
    plans = list(
        session.scalars(
            select(MedicationPlan).where(
                MedicationPlan.patient_id == patient_id,
                MedicationPlan.source_type == TESTBED_SCENARIO_SOURCE,
                MedicationPlan.end_date >= schedule_date,
            )
        ).all()
    )
    for plan in plans:
        if plan.start_date < schedule_date:
            _delete_owned_scenario_events_from_date(
                session,
                plan=plan,
                schedule_date=schedule_date,
            )
            plan.end_date = schedule_date - timedelta(days=1)
        else:
            _delete_owned_scenario_plan(session, plan)
    session.flush()


def _delete_owned_scenario_events_from_date(
    session: Session,
    *,
    plan: MedicationPlan,
    schedule_date: date,
) -> None:
    dose_event_ids = list(
        session.scalars(
            select(DoseEvent.id).where(
                DoseEvent.plan_id == plan.id,
                DoseEvent.scheduled_for
                >= datetime.combine(schedule_date, time.min),
            )
        ).all()
    )
    _delete_owned_scenario_events(session, dose_event_ids)


def _delete_owned_scenario_plan(session: Session, plan: MedicationPlan) -> None:
    dose_event_ids = list(
        session.scalars(
            select(DoseEvent.id).where(DoseEvent.plan_id == plan.id)
        ).all()
    )
    _delete_owned_scenario_events(session, dose_event_ids)
    session.execute(
        delete(DoseSchedule).where(DoseSchedule.plan_id == plan.id)
    )
    session.delete(plan)


def _delete_owned_scenario_events(
    session: Session,
    dose_event_ids: list[int],
) -> None:
    if dose_event_ids:
        notification_ids = list(
            session.scalars(
                select(Notification.id).where(
                    Notification.related_dose_event_id.in_(dose_event_ids)
                )
            ).all()
        )
        chat_message_ids = list(
            session.scalars(
                select(ChatMessage.id).where(
                    ChatMessage.related_dose_event_id.in_(dose_event_ids)
                )
            ).all()
        )
        session.execute(
            update(AgentJob)
            .where(AgentJob.related_dose_event_id.in_(dose_event_ids))
            .values(related_dose_event_id=None)
        )
        session.execute(
            update(SideEffectRecord)
            .where(SideEffectRecord.related_dose_event_id.in_(dose_event_ids))
            .values(related_dose_event_id=None)
        )
        if notification_ids:
            session.execute(
                delete(Notification).where(Notification.id.in_(notification_ids))
            )
        if chat_message_ids:
            session.execute(
                update(ChatMessage)
                .where(ChatMessage.reply_to_message_id.in_(chat_message_ids))
                .values(reply_to_message_id=None)
            )
            session.execute(
                delete(ChatMessage).where(ChatMessage.id.in_(chat_message_ids))
            )
        session.execute(
            delete(MissedDoseFlag).where(
                MissedDoseFlag.related_dose_event_id.in_(dose_event_ids)
            )
        )
        session.execute(
            delete(DoseEvent).where(DoseEvent.id.in_(dose_event_ids))
        )
