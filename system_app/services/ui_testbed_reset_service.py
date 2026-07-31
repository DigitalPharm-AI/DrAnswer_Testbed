from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.settings import get_settings
from system_app.models import AgentJob, ChatMessage
from system_app.services.medication_plan_service import reset_simulation_state
from system_app.services.nutrition_service import ensure_nutrition_profile

_BUSY_AGENT_JOB_STATUSES = ("pending", "running", "retry_requested")


def testbed_reset_is_enabled() -> bool:
    settings = get_settings()
    return bool(settings.testbed_reset_enabled and not settings.is_production())


def testbed_reset_is_busy(session: Session) -> bool:
    settings = get_settings()
    pending_chat = session.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.patient_id == settings.patient_id,
            ChatMessage.role == "user",
            ChatMessage.processing_status == "pending",
        )
        .limit(1)
    )
    if pending_chat is not None:
        return True

    active_agent_job = session.scalar(
        select(AgentJob.id)
        .where(AgentJob.status.in_(_BUSY_AGENT_JOB_STATUSES))
        .limit(1)
    )
    return active_agent_job is not None


def reset_testbed_state(session: Session) -> None:
    """Reset Backend test data while preserving catalogs and API audits."""

    reset_simulation_state(session, commit=False)
    ensure_nutrition_profile(
        session,
        patient_id=get_settings().patient_id,
        persist=True,
    )
    session.flush()
