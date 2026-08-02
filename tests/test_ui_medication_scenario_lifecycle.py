from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from shared.settings import get_settings
from system_app.models import (
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    Notification,
    ReminderPolicy,
)
from system_app.services.clock_service import ensure_clock
from system_app.services.dose_event_service import ensure_day_events
from system_app.services.patient_profile_service import simulation_readiness
from system_app.services.ui_medication_scenario_service import (
    TESTBED_SCENARIO_END_DATE,
    TESTBED_SCENARIO_SOURCE,
    active_test_medication_scenario,
    apply_test_medication_scenario,
    ensure_initial_testbed_scenario_state,
    medication_events_for_date,
    notification_policy_end_date,
    seed_test_medication_scenarios,
)
from system_app.services.ui_view_service import dashboard_view
from tests.helpers import build_system_engine


def _sessions(tmp_path, name: str):
    engine, _cleanup = build_system_engine(
        "ui_medication_scenario"
    )
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def _add_plan(
    session,
    *,
    patient_id: str,
    medication_name: str,
    source_type: str,
    source_key: str,
    start_date: date,
    end_date: date,
    slot_label: str = "아침",
    scheduled_time: str = "08:00",
) -> MedicationPlan:
    plan = MedicationPlan(
        patient_id=patient_id,
        medication_name=medication_name,
        dosage="1정",
        instructions="",
        treatment_area="테스트",
        source_type=source_type,
        source_key=source_key,
        start_date=start_date,
        end_date=end_date,
        active=True,
    )
    session.add(plan)
    session.flush()
    session.add(
        DoseSchedule(
            plan_id=plan.id,
            slot_label=slot_label,
            scheduled_time=scheduled_time,
        )
    )
    session.flush()
    return plan


def _events_on(session, target_date: date) -> list[DoseEvent]:
    return list(
        session.scalars(
            select(DoseEvent)
            .where(
                DoseEvent.scheduled_for
                >= datetime.combine(target_date, time.min),
                DoseEvent.scheduled_for
                <= datetime.combine(target_date, time.max),
            )
            .order_by(DoseEvent.scheduled_for, DoseEvent.id)
        ).all()
    )


def test_manual_active_plan_is_ready_without_separate_phr_registration(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path, "manual_plan_readiness")
    patient_id = get_settings().patient_id

    with sessions() as session:
        current_date = ensure_clock(session).current_time.date()
        _add_plan(
            session,
            patient_id=patient_id,
            medication_name="수동 등록 혈압약",
            source_type="manual",
            source_key="manual-readiness",
            start_date=current_date,
            end_date=current_date + timedelta(days=30),
        )
        session.commit()

        readiness = simulation_readiness(session)

    assert readiness == {
        "ready": True,
        "reason": "active_medication_plan_ready",
        "message": "활성 복약 일정이 등록되어 시뮬레이션을 실행할 수 있습니다.",
    }


def test_scenario_continues_next_day_with_idempotent_events_and_readiness(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path, "scenario_next_day")

    with sessions() as session:
        seed_test_medication_scenarios(session)
        clock = ensure_clock(session)
        first_date = clock.current_time.date()
        next_date = first_date + timedelta(days=1)

        apply_test_medication_scenario(
            session,
            scenario_id="tb-diabetes-v1",
            schedule_date=first_date,
        )
        plan = session.scalar(
            select(MedicationPlan).where(
                MedicationPlan.source_type == TESTBED_SCENARIO_SOURCE
            )
        )
        assert plan is not None
        assert plan.end_date == TESTBED_SCENARIO_END_DATE

        clock.current_time = datetime.combine(next_date, time(0, 30))
        assert ensure_initial_testbed_scenario_state(session) is False
        assert ensure_initial_testbed_scenario_state(session) is False
        session.flush()

        next_events = _events_on(session, next_date)
        assert len(next_events) == 1
        assert next_events[0].medication_name == "메트포르민 500mg"
        assert simulation_readiness(session)["ready"] is True
        assert active_test_medication_scenario(
            session,
            next_date,
        )["scenario_id"] == "tb-diabetes-v1"

        dashboard = dashboard_view(session)
        assert dashboard["simulation_ready"] is True
        assert dashboard["active_scenario"]["scenario_id"] == "tb-diabetes-v1"
        assert [
            item["medication_name"] for item in dashboard["medications"]
        ] == ["메트포르민 500mg"]


def test_testbed_scenario_provisions_active_policies_idempotently(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path, "scenario_policy_provisioning")
    patient_id = get_settings().patient_id

    with sessions() as session:
        seed_test_medication_scenarios(session)
        schedule_date = ensure_clock(session).current_time.date()

        apply_test_medication_scenario(
            session,
            scenario_id="tb-combined-v1",
            schedule_date=schedule_date,
        )
        first_policies = list(
            session.scalars(
                select(ReminderPolicy)
                .where(
                    ReminderPolicy.patient_id == patient_id,
                    ReminderPolicy.active.is_(True),
                )
                .order_by(ReminderPolicy.slot_label)
            ).all()
        )
        first_ids = {policy.public_id for policy in first_policies}
        for policy in first_policies:
            policy.effective_end_date = TESTBED_SCENARIO_END_DATE
        session.flush()

        apply_test_medication_scenario(
            session,
            scenario_id="tb-combined-v1",
            schedule_date=schedule_date,
        )
        second_policies = list(
            session.scalars(
                select(ReminderPolicy).where(
                    ReminderPolicy.patient_id == patient_id,
                    ReminderPolicy.active.is_(True),
                )
            ).all()
        )

        assert {policy.slot_label for policy in first_policies} == {
            "아침",
            "점심",
            "저녁",
        }
        assert len(first_policies) == 3
        assert {policy.public_id for policy in second_policies} == first_ids
        assert all(
            policy.effective_start_date == schedule_date
            and policy.effective_end_date
            == notification_policy_end_date(schedule_date)
            for policy in second_policies
        )


def test_testbed_policy_end_date_is_one_inclusive_calendar_month() -> None:
    assert notification_policy_end_date(date(2026, 4, 20)) == date(
        2026, 5, 19
    )
    assert notification_policy_end_date(date(2026, 1, 31)) == date(
        2026, 2, 27
    )


def test_next_day_replacement_preserves_history_and_removes_future_outputs(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path, "scenario_replacement")
    patient_id = get_settings().patient_id

    with sessions() as session:
        seed_test_medication_scenarios(session)
        first_date = ensure_clock(session).current_time.date()
        replacement_date = first_date + timedelta(days=1)
        later_date = replacement_date + timedelta(days=1)
        manual = _add_plan(
            session,
            patient_id=patient_id,
            medication_name="수동 등록약",
            source_type="manual",
            source_key="",
            start_date=first_date,
            end_date=later_date,
            scheduled_time="12:00",
        )
        other_patient = _add_plan(
            session,
            patient_id="other-patient",
            medication_name="다른 환자 테스트약",
            source_type=TESTBED_SCENARIO_SOURCE,
            source_key="tb-breast-cancer-v1",
            start_date=first_date,
            end_date=TESTBED_SCENARIO_END_DATE,
            scheduled_time="18:00",
        )

        apply_test_medication_scenario(
            session,
            scenario_id="tb-diabetes-v1",
            schedule_date=first_date,
        )
        ensure_day_events(session, replacement_date)
        ensure_day_events(session, later_date)

        old_plan = session.scalar(
            select(MedicationPlan).where(
                MedicationPlan.patient_id == patient_id,
                MedicationPlan.source_key == "tb-diabetes-v1",
            )
        )
        assert old_plan is not None
        old_events = list(
            session.scalars(
                select(DoseEvent)
                .where(DoseEvent.plan_id == old_plan.id)
                .order_by(DoseEvent.scheduled_for)
            ).all()
        )
        assert len(old_events) == 3
        old_events[0].status = "taken"
        old_events[0].taken_at = old_events[0].scheduled_for
        retained_event_id = old_events[0].id
        removed_event_public_ids = {
            old_events[1].public_id,
            old_events[2].public_id,
        }
        retained_notification = Notification(
            patient_id=patient_id,
            notification_type="medication_alert",
            title="과거 시나리오 알림",
            body="교체 이전 이력은 보존되어야 합니다.",
            visible_at=old_events[0].scheduled_for,
            related_dose_event_id=old_events[0].id,
        )
        retained_chat = ChatMessage(
            patient_id=patient_id,
            ai_request_id="retained-scenario-output",
            role="assistant",
            sender_type="assistant",
            category="missed_dose",
            message_type="text",
            content="교체 이전 시나리오에서 생성된 메시지",
            related_dose_event_id=old_events[0].id,
        )
        future_notification = Notification(
            patient_id=patient_id,
            notification_type="medication_alert",
            title="기존 시나리오 알림",
            body="교체 이후에는 제거되어야 합니다.",
            visible_at=old_events[1].scheduled_for,
            related_dose_event_id=old_events[1].id,
        )
        future_chat = ChatMessage(
            patient_id=patient_id,
            ai_request_id="old-scenario-output",
            role="assistant",
            sender_type="assistant",
            category="missed_dose",
            message_type="text",
            content="기존 시나리오에서 생성된 메시지",
            related_dose_event_id=old_events[1].id,
        )
        session.add_all(
            [
                retained_notification,
                retained_chat,
                future_notification,
                future_chat,
            ]
        )
        session.flush()
        retained_notification_id = retained_notification.id
        retained_chat_id = retained_chat.id
        future_notification_id = future_notification.id
        future_chat_id = future_chat.id

        apply_test_medication_scenario(
            session,
            scenario_id="tb-hypertension-v1",
            schedule_date=replacement_date,
        )
        session.flush()

        assert old_plan.end_date == first_date
        retained = session.get(DoseEvent, retained_event_id)
        assert retained is not None
        assert retained.status == "taken"
        assert session.get(Notification, retained_notification_id) is not None
        assert session.get(ChatMessage, retained_chat_id) is not None
        assert (
            session.query(DoseEvent)
            .filter(DoseEvent.public_id.in_(removed_event_public_ids))
            .count()
            == 0
        )
        assert session.get(Notification, future_notification_id) is None
        assert session.get(ChatMessage, future_chat_id) is None
        assert session.get(MedicationPlan, manual.id) is not None
        assert session.get(MedicationPlan, other_patient.id) is not None

        new_plan = session.scalar(
            select(MedicationPlan).where(
                MedicationPlan.patient_id == patient_id,
                MedicationPlan.source_key == "tb-hypertension-v1",
            )
        )
        assert new_plan is not None
        assert new_plan.start_date == replacement_date
        assert new_plan.end_date == TESTBED_SCENARIO_END_DATE
        assert active_test_medication_scenario(
            session,
            first_date,
        )["scenario_id"] == "tb-diabetes-v1"
        assert active_test_medication_scenario(
            session,
            replacement_date,
        )["scenario_id"] == "tb-hypertension-v1"
        assert {
            item["medication_name"]
            for item in medication_events_for_date(
                session,
                replacement_date,
            )
        } == {"수동 등록약", "암로디핀 5mg"}


def test_same_day_reapply_replaces_testbed_only_without_duplicates(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path, "scenario_same_day")
    patient_id = get_settings().patient_id

    with sessions() as session:
        seed_test_medication_scenarios(session)
        schedule_date = ensure_clock(session).current_time.date()
        manual = _add_plan(
            session,
            patient_id=patient_id,
            medication_name="수동 등록약",
            source_type="manual",
            source_key="manual-plan",
            start_date=schedule_date,
            end_date=schedule_date,
            scheduled_time="12:00",
        )
        other_patient = _add_plan(
            session,
            patient_id="other-patient",
            medication_name="다른 환자 테스트약",
            source_type=TESTBED_SCENARIO_SOURCE,
            source_key="tb-kidney-cancer-v1",
            start_date=schedule_date,
            end_date=TESTBED_SCENARIO_END_DATE,
            scheduled_time="13:00",
        )
        apply_test_medication_scenario(
            session,
            scenario_id="tb-diabetes-v1",
            schedule_date=schedule_date,
        )

        apply_test_medication_scenario(
            session,
            scenario_id="tb-hypertension-v1",
            schedule_date=schedule_date,
        )
        session.flush()

        current_patient_testbed_plans = list(
            session.scalars(
                select(MedicationPlan).where(
                    MedicationPlan.patient_id == patient_id,
                    MedicationPlan.source_type == TESTBED_SCENARIO_SOURCE,
                )
            ).all()
        )
        assert len(current_patient_testbed_plans) == 1
        assert current_patient_testbed_plans[0].source_key == (
            "tb-hypertension-v1"
        )
        assert session.get(MedicationPlan, manual.id) is not None
        assert session.get(MedicationPlan, other_patient.id) is not None
        assert {
            item["medication_name"]
            for item in medication_events_for_date(session, schedule_date)
        } == {"수동 등록약", "암로디핀 5mg"}
