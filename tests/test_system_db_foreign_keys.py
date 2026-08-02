from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from system_app.models import (
    AgentJob,
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    MissedDoseFlag,
    Notification,
    SideEffectRecord,
)
from system_app.services.medication_plan_service import (
    create_medication_plan,
    delete_medication_plan,
    reset_simulation_state,
)
from tests.helpers import build_system_engine


@pytest.fixture
def foreign_key_session():
    engine, cleanup = build_system_engine("foreign_keys")
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    try:
        with session_factory() as session:
            yield session
    finally:
        cleanup()


def _build_linked_history(session):
    plan = create_medication_plan(
        session,
        medication_name="외래키 검증약",
        dosage="1정",
        start_date=date(2026, 4, 20),
        end_date=date(2026, 4, 20),
        times_csv="08:00",
    )
    event = session.scalar(select(DoseEvent).where(DoseEvent.plan_id == plan.id))
    assert event is not None

    notification = Notification(
        patient_id="demo-patient",
        notification_type="medication_alert",
        title="복약 알림",
        body="외래키 검증",
        visible_at=event.scheduled_for,
        related_dose_event_id=event.id,
    )
    source_message = ChatMessage(
        patient_id="demo-patient",
        role="assistant",
        sender_type="assistant",
        category="missed_dose",
        content="복약 여부를 알려주세요.",
        related_dose_event_id=event.id,
    )
    session.add_all([notification, source_message])
    session.flush()

    reply = ChatMessage(
        patient_id="demo-patient",
        role="user",
        sender_type="patient",
        category="chat",
        content="확인했습니다.",
        reply_to_message_id=source_message.id,
    )
    job = AgentJob(
        job_type="missed_dose",
        payload_json="{}",
        related_dose_event_id=event.id,
    )
    side_effect = SideEffectRecord(
        patient_id="demo-patient",
        symptom_text="메스꺼움",
        related_dose_event_id=event.id,
    )
    flag = MissedDoseFlag(
        patient_id="demo-patient",
        flag_date=event.scheduled_for.date(),
        related_dose_event_id=event.id,
        activated_at=datetime(2026, 4, 20, 8, 30),
    )
    session.add_all([reply, job, side_effect, flag])
    session.commit()
    return plan, event, notification, source_message, reply, job, side_effect


def test_postgresql_session_factory_enforces_foreign_keys(foreign_key_session):
    session = foreign_key_session
    assert session.scalar(text("SHOW server_version")) != ""

    session.add(DoseSchedule(plan_id=999_999, slot_label="아침 08:00", scheduled_time="08:00"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_delete_medication_plan_preserves_history_without_dangling_references(foreign_key_session):
    session = foreign_key_session
    plan, event, notification, source_message, reply, job, side_effect = _build_linked_history(session)
    plan_id = plan.id
    event_id = event.id
    notification_id = notification.id
    source_message_id = source_message.id

    assert delete_medication_plan(session, plan_id) is True

    assert session.get(MedicationPlan, plan_id) is None
    assert session.get(DoseEvent, event_id) is None
    assert session.get(Notification, notification_id) is None
    assert session.get(ChatMessage, source_message_id) is None
    assert session.scalar(select(MissedDoseFlag).where(MissedDoseFlag.related_dose_event_id == event_id)) is None

    session.refresh(reply)
    session.refresh(job)
    session.refresh(side_effect)
    assert reply.reply_to_message_id is None
    assert job.related_dose_event_id is None
    assert side_effect.related_dose_event_id is None


def test_reset_simulation_state_deletes_fk_children_before_parents(foreign_key_session):
    session = foreign_key_session
    _build_linked_history(session)

    reset_simulation_state(session)

    for model in (
        Notification,
        ChatMessage,
        AgentJob,
        SideEffectRecord,
        MissedDoseFlag,
        DoseEvent,
        DoseSchedule,
        MedicationPlan,
    ):
        assert session.scalar(select(model)) is None
