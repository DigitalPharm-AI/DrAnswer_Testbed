from __future__ import annotations

from sqlalchemy.orm import Session

from system_app.services.agent_client import AgentServiceError
from system_app.services.clock_service import ensure_clock, pause_simulation_clock
from system_app.services.notification_service import create_notification
from system_app.services.timeline_service import add_chat_message


def present_agent_error(
    session: Session,
    source_event_type: str,
    error: Exception,
    *,
    user_message: str,
    pause_clock: bool = False,
    add_chat: bool = False,
    related_dose_event_id: int | None = None,
    metadata: dict | None = None,
) -> None:
    error_type = "agent_runtime_error"

    if isinstance(error, AgentServiceError):
        error_type = error.error_type

    clock = pause_simulation_clock(session) if pause_clock else ensure_clock(session)

    create_notification(
        session,
        notification_type="agent_error",
        title="AI 에이전트 오류",
        body=user_message,
        visible_at=clock.current_time,
        related_dose_event_id=related_dose_event_id,
        metadata={
            "source_event_type": source_event_type,
            "error_type": error_type,
            **(metadata or {}),
        },
    )

    if add_chat:
        add_chat_message(
            session,
            role="assistant",
            content=user_message,
            sender_type="assistant",
            category="error",
            related_dose_event_id=related_dose_event_id,
        )
