from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.idempotency import (
    COMPLETED,
    FINAL_FAILED,
    RETRYABLE_FAILED,
)
from agent_app.observability.outbox import (
    COMPLETED as EXPORT_COMPLETED,
    DEAD as EXPORT_DEAD,
    SKIPPED as EXPORT_SKIPPED,
    enqueue_trace_delete,
)
from agent_app.persistence.models import (
    AgentAsyncTask,
    AgentBackendWriteRequest,
    AgentPatientLock,
    AgentFeedbackLink,
    AgentObservabilityExport,
    AgentPendingAction,
    AgentPendingSelection,
    AgentProCtcaeSurvey,
    AgentRunStep,
    AgentRunTrace,
    AgentSyncRequest,
    AgentToolExecution,
)
from shared.settings import get_settings
from shared.time_utils import utc_now


def purge_expired_agent_state(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    current = now or utc_now()
    settings = get_settings()
    with session_factory() as session:
        expired_pending_actions = session.execute(
            update(AgentPendingAction)
            .where(
                AgentPendingAction.status.in_(
                    ("PENDING", "APPROVED", "SENDING")
                ),
                AgentPendingAction.action_expires_at <= current,
            )
            .values(
                status="EXPIRED",
                error_code="ACTION_EXPIRED",
                resolved_at=current,
                updated_at=current,
            )
        ).rowcount
        expired_pro_ctcae_surveys = session.execute(
            update(AgentProCtcaeSurvey)
            .where(
                AgentProCtcaeSurvey.status.in_(
                    (
                        "AWAITING_RESPONSE",
                        "COMPLETED",
                        "APPROVAL_PENDING",
                    )
                ),
                AgentProCtcaeSurvey.response_expires_at <= current,
            )
            .values(
                status="EXPIRED",
                resolved_at=current,
                updated_at=current,
            )
        ).rowcount
        expired_pending_selections = session.execute(
            update(AgentPendingSelection)
            .where(
                AgentPendingSelection.status.in_(
                    ("PENDING", "RESOLVED")
                ),
                AgentPendingSelection.selection_expires_at
                <= current,
            )
            .values(
                status="EXPIRED",
                resolved_at=current,
                updated_at=current,
                version=AgentPendingSelection.version + 1,
            )
        ).rowcount

        expired_trace_ids = list(
            session.scalars(
                select(AgentRunTrace.trace_id).where(
                    AgentRunTrace.expires_at <= current
                )
            ).all()
        )
        for trace_id in expired_trace_ids:
            enqueue_trace_delete(
                session,
                trace_id=trace_id,
                settings=settings,
                recorded_at=current,
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
        deleted_backend_write_requests = session.execute(
            delete(AgentBackendWriteRequest).where(
                AgentBackendWriteRequest.expires_at <= current
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
                AgentPendingAction.status.notin_(
                    ("PENDING", "APPROVED", "SENDING")
                ),
            )
        ).rowcount
        deleted_pro_ctcae_surveys = session.execute(
            delete(AgentProCtcaeSurvey).where(
                AgentProCtcaeSurvey.expires_at <= current,
                AgentProCtcaeSurvey.status.notin_(
                    (
                        "AWAITING_RESPONSE",
                        "COMPLETED",
                        "APPROVAL_PENDING",
                    )
                ),
            )
        ).rowcount
        deleted_pending_selections = session.execute(
            delete(AgentPendingSelection).where(
                AgentPendingSelection.expires_at <= current,
                AgentPendingSelection.status.notin_(
                    ("PENDING", "RESOLVED")
                ),
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
        deleted_async_tasks = session.execute(
            delete(AgentAsyncTask).where(
                AgentAsyncTask.expires_at <= current,
                AgentAsyncTask.status.in_(
                    ("done", "dead", "failed")
                ),
            )
        ).rowcount
        deleted_conversation_locks = session.execute(
            delete(AgentPatientLock).where(
                AgentPatientLock.lease_expires_at <= current
            )
        ).rowcount
        deleted_observability_exports = session.execute(
            delete(AgentObservabilityExport).where(
                AgentObservabilityExport.expires_at <= current,
                AgentObservabilityExport.status.in_(
                    (
                        EXPORT_COMPLETED,
                        EXPORT_DEAD,
                        EXPORT_SKIPPED,
                    )
                ),
            )
        ).rowcount
        session.commit()

    return {
        "expired_pending_actions": int(expired_pending_actions or 0),
        "expired_pro_ctcae_surveys": int(
            expired_pro_ctcae_surveys or 0
        ),
        "expired_pending_selections": int(
            expired_pending_selections or 0
        ),
        "deleted_pending_actions": int(deleted_pending_actions or 0),
        "deleted_pro_ctcae_surveys": int(
            deleted_pro_ctcae_surveys or 0
        ),
        "deleted_pending_selections": int(
            deleted_pending_selections or 0
        ),
        "deleted_feedback_links": int(deleted_feedback_links or 0),
        "deleted_tool_executions": int(deleted_tool_executions or 0),
        "deleted_backend_write_requests": int(
            deleted_backend_write_requests or 0
        ),
        "deleted_run_steps": int(deleted_steps or 0),
        "deleted_run_traces": int(deleted_traces or 0),
        "deleted_sync_requests": int(deleted_sync_requests or 0),
        "deleted_async_tasks": int(deleted_async_tasks or 0),
        "deleted_conversation_locks": int(deleted_conversation_locks or 0),
        "deleted_observability_exports": int(
            deleted_observability_exports or 0
        ),
    }
