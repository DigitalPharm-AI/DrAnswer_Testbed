from __future__ import annotations

from sqlalchemy.orm import Session

from shared.redaction import safe_exception_summary
from shared.time_utils import utc_now
from system_app.services.agent_client import AgentServiceError
from system_app.services.audit_service import create_agent_decision_audit
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
    trace_id = None
    agent_name = "system"
    decision_type = "agent_call_failed"
    error_type = "agent_runtime_error"
    error_message = safe_exception_summary(error)

    if isinstance(error, AgentServiceError):
        trace_id = error.trace_id
        agent_name = error.agent_name or "system"
        decision_type = error.decision_type or "agent_call_failed"
        error_type = error.error_type
        error_message = safe_exception_summary(error)

    create_agent_decision_audit(
        session,
        trace_id=trace_id or f"error-{utc_now().timestamp()}",
        agent_name=agent_name,
        prompt_version_id="n/a",
        decision_type=decision_type,
        structured_payload={},
        human_summary=user_message,
        applied=False,
        error_message=error_message,
        source_event_type=source_event_type,
    )

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
            "trace_id": trace_id,
            "decision_type": decision_type,
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
