from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.idempotency import (
    COMPLETED,
    FINAL_FAILED,
    RETRYABLE_FAILED,
)
from agent_app.persistence.models import (
    AgentConversationLock,
    AgentFeedbackLink,
    AgentPendingAction,
    AgentRunStep,
    AgentRunTrace,
    AgentSyncRequest,
    AgentToolExecution,
)
from shared.time_utils import utc_now


def purge_expired_agent_state(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    current = now or utc_now()
    with session_factory() as session:
        expired_pending_actions = session.execute(
            update(AgentPendingAction)
            .where(
                AgentPendingAction.status == "PENDING",
                AgentPendingAction.action_expires_at <= current,
            )
            .values(
                status="EXPIRED",
                error_code="ACTION_EXPIRED",
                resolved_at=current,
                updated_at=current,
            )
        ).rowcount

        expired_trace_ids = select(AgentRunTrace.trace_id).where(
            AgentRunTrace.expires_at <= current
        )
        deleted_steps = session.execute(
            delete(AgentRunStep).where(
                or_(
                    AgentRunStep.expires_at <= current,
                    AgentRunStep.trace_id.in_(expired_trace_ids),
                )
            )
        ).rowcount
        deleted_tool_executions = session.execute(
            delete(AgentToolExecution).where(
                or_(
                    AgentToolExecution.expires_at <= current,
                    AgentToolExecution.trace_id.in_(expired_trace_ids),
                )
            )
        ).rowcount
        deleted_traces = session.execute(
            delete(AgentRunTrace).where(AgentRunTrace.expires_at <= current)
        ).rowcount
        deleted_feedback_links = session.execute(
            delete(AgentFeedbackLink).where(
                AgentFeedbackLink.expires_at <= current
            )
        ).rowcount
        deleted_pending_actions = session.execute(
            delete(AgentPendingAction).where(
                AgentPendingAction.expires_at <= current,
                AgentPendingAction.status.notin_(("PENDING", "EXECUTING")),
            )
        ).rowcount
        deleted_sync_requests = session.execute(
            delete(AgentSyncRequest).where(
                AgentSyncRequest.expires_at <= current,
                AgentSyncRequest.status.in_(
                    (COMPLETED, RETRYABLE_FAILED, FINAL_FAILED)
                ),
            )
        ).rowcount
        deleted_conversation_locks = session.execute(
            delete(AgentConversationLock).where(
                AgentConversationLock.lease_expires_at <= current
            )
        ).rowcount
        session.commit()

    return {
        "expired_pending_actions": int(expired_pending_actions or 0),
        "deleted_pending_actions": int(deleted_pending_actions or 0),
        "deleted_feedback_links": int(deleted_feedback_links or 0),
        "deleted_tool_executions": int(deleted_tool_executions or 0),
        "deleted_run_steps": int(deleted_steps or 0),
        "deleted_run_traces": int(deleted_traces or 0),
        "deleted_sync_requests": int(deleted_sync_requests or 0),
        "deleted_conversation_locks": int(deleted_conversation_locks or 0),
    }
