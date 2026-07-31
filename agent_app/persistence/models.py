from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from shared.time_utils import utc_now


def utcnow() -> datetime:
    return utc_now()


class Base(DeclarativeBase):
    pass


class AgentAsyncTask(Base):
    __tablename__ = "agent_async_tasks"
    __table_args__ = (
        Index(
            "uq_agent_async_tasks_deduplication_key",
            "deduplication_key",
            unique=True,
            postgresql_where=text("deduplication_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    deduplication_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    task_type: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True, default="pending")
    payload_json: Mapped[str] = mapped_column(Text)
    callback_context_json: Mapped[str] = mapped_column(Text, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str] = mapped_column(Text, default="")
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    run_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str] = mapped_column(String(160), default="")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        index=True,
        default=lambda: utcnow() + timedelta(days=30),
    )


class AgentWorkerHeartbeat(Base):
    __tablename__ = "agent_worker_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), index=True, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_task_request_id: Mapped[str] = mapped_column(String(160), default="")
    current_task_type: Mapped[str] = mapped_column(String(60), default="")
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")


class AgentSyncRequest(Base):
    __tablename__ = "agent_sync_requests"
    __table_args__ = (
        UniqueConstraint("api_path", "request_id", name="uq_agent_sync_requests_path_request"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_path: Mapped[str] = mapped_column(String(160), index=True)
    request_id: Mapped[str] = mapped_column(String(160), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    patient_id: Mapped[str] = mapped_column(String(160), index=True)
    message_id: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), index=True, default="RECEIVED")
    attempt_epoch: Mapped[int] = mapped_column(Integer, default=0)
    trace_id: Mapped[str] = mapped_column(String(160), default="")
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[str] = mapped_column(Text, default="")
    last_error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentPatientLock(Base):
    __tablename__ = "agent_patient_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    request_id: Mapped[str] = mapped_column(String(160), index=True)
    attempt_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRunTrace(Base):
    __tablename__ = "agent_run_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    request_id: Mapped[str] = mapped_column(String(160), index=True)
    api_path: Mapped[str] = mapped_column(String(160), default="")
    message_id: Mapped[str] = mapped_column(String(160), default="")
    patient_id_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    environment: Mapped[str] = mapped_column(String(40), default="")
    release_version: Mapped[str] = mapped_column(String(120), default="")
    workflow_name: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="PROCESSING")
    agent_name: Mapped[str] = mapped_column(String(120), default="")
    decision_type: Mapped[str] = mapped_column(String(80), default="")
    route: Mapped[str] = mapped_column(String(120), default="")
    final_answer_source: Mapped[str] = mapped_column(String(160), default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    prompt_version_id: Mapped[str] = mapped_column(String(120), default="")
    provider: Mapped[str] = mapped_column(String(80), default="")
    model_tier: Mapped[str] = mapped_column(String(40), default="")
    model_id: Mapped[str] = mapped_column(String(255), default="")
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    output_hash: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tool_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentRunStep(Base):
    __tablename__ = "agent_run_steps"
    __table_args__ = (
        UniqueConstraint("trace_id", "sequence", name="uq_agent_run_steps_trace_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_run_traces.trace_id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    observation_id: Mapped[str] = mapped_column(String(64), default="")
    parent_observation_id: Mapped[str] = mapped_column(String(64), default="")
    observation_type: Mapped[str] = mapped_column(String(40), default="span")
    trace_attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    step_type: Mapped[str] = mapped_column(String(60), index=True)
    step_name: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(32), default="")
    tool_name: Mapped[str] = mapped_column(String(160), default="", index=True)
    tool_version: Mapped[str] = mapped_column(String(80), default="")
    argument_schema_version: Mapped[str] = mapped_column(String(80), default="")
    argument_hash: Mapped[str] = mapped_column(String(64), default="")
    side_effect_level: Mapped[str] = mapped_column(String(40), default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    time_to_first_token_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    level: Mapped[str] = mapped_column(String(20), default="DEFAULT")
    status_message: Mapped[str] = mapped_column(Text, default="")
    prompt_version_id: Mapped[str] = mapped_column(String(120), default="")
    provider: Mapped[str] = mapped_column(String(80), default="")
    model_id: Mapped[str] = mapped_column(String(255), default="")
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    output_hash: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    model_parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    usage_details_json: Mapped[str] = mapped_column(Text, default="{}")
    cost_details_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_ciphertext: Mapped[str] = mapped_column(Text, default="")
    evidence_hash: Mapped[str] = mapped_column(String(64), default="")
    encryption_key_id: Mapped[str] = mapped_column(
        String(120),
        default="",
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completion_start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentObservabilityExport(Base):
    """Durable, PHI-free outbox for external observability sinks."""

    __tablename__ = "agent_observability_exports"
    __table_args__ = (
        UniqueConstraint(
            "destination",
            "event_key",
            name="uq_agent_observability_exports_destination_event",
        ),
        Index(
            "ix_agent_observability_exports_due",
            "destination",
            "status",
            "next_attempt_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destination: Mapped[str] = mapped_column(
        String(40),
        default="langfuse",
    )
    event_key: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    trace_id: Mapped[str] = mapped_column(String(160), index=True)
    trace_attempt_number: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(
        String(32),
        index=True,
        default="PENDING",
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=12)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )
    locked_by: Mapped[str] = mapped_column(String(160), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )
    last_error_code: Mapped[str] = mapped_column(String(80), default="")
    last_error_message: Mapped[str] = mapped_column(Text, default="")
    remote_trace_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    exported_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentToolExecution(Base):
    __tablename__ = "agent_tool_executions"
    __table_args__ = (
        UniqueConstraint(
            "trace_id",
            "trace_attempt_number",
            "tool_call_id",
            name="uq_agent_tool_executions_trace_attempt_call",
        ),
        Index(
            "ix_agent_tool_executions_request_status",
            "request_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(160), index=True)
    request_id: Mapped[str] = mapped_column(String(160), index=True)
    patient_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(180))
    trace_attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    tool_name: Mapped[str] = mapped_column(String(160), index=True)
    tool_version: Mapped[str] = mapped_column(String(80), default="")
    argument_schema_version: Mapped[str] = mapped_column(String(80), default="")
    argument_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), index=True, default="PENDING")
    side_effect_level: Mapped[str] = mapped_column(String(40), default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    response_hash: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentBackendWriteRequest(Base):
    """Restart-safe state for one idempotent AI -> Backend write request."""

    __tablename__ = "agent_backend_write_requests"
    __table_args__ = (
        UniqueConstraint(
            "request_id",
            name="uq_agent_backend_write_requests_request",
        ),
        UniqueConstraint(
            "source_chat_request_id",
            "tool_name",
            "argument_hash",
            "trusted_context_hash",
            name="uq_agent_backend_write_requests_logical_call",
        ),
        Index(
            "ix_agent_backend_write_requests_source_status",
            "source_chat_request_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(160), index=True)
    source_chat_request_id: Mapped[str] = mapped_column(String(160), index=True)
    patient_id_hash: Mapped[str] = mapped_column(String(64), default="")
    trusted_context_hash: Mapped[str] = mapped_column(String(64))
    tool_call_id: Mapped[str] = mapped_column(String(180))
    tool_name: Mapped[str] = mapped_column(String(160), index=True)
    argument_hash: Mapped[str] = mapped_column(String(64))
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="PREPARED")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[str] = mapped_column(Text, default="")
    response_hash: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentPendingAction(Base):
    __tablename__ = "agent_pending_actions"
    __table_args__ = (
        Index(
            "uq_agent_pending_actions_active_patient",
            "patient_id_hash",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING', 'APPROVED', 'SENDING')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    source_chat_request_id: Mapped[str] = mapped_column(String(160), index=True)
    source_message_id: Mapped[str] = mapped_column(String(160))
    trace_id: Mapped[str] = mapped_column(String(160), index=True)
    patient_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    action_type: Mapped[str] = mapped_column(String(60))
    action_name: Mapped[str] = mapped_column(String(160), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(180), default="")
    action_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    target_resource_type: Mapped[str] = mapped_column(String(80), default="")
    target_record_id: Mapped[str] = mapped_column(String(160), default="")
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_json: Mapped[str] = mapped_column(Text, default="{}")
    payload_ciphertext: Mapped[str] = mapped_column(Text, default="")
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    argument_hash: Mapped[str] = mapped_column(String(64), default="")
    encryption_key_id: Mapped[str] = mapped_column(String(120), default="")
    approval_key_hash: Mapped[str] = mapped_column(
        String(64),
        default="",
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), index=True, default="PENDING")
    confirmation_message_id: Mapped[str] = mapped_column(String(160), default="")
    approved_source_chat_request_id: Mapped[str] = mapped_column(
        String(160),
        default="",
    )
    write_request_id: Mapped[str] = mapped_column(
        String(160),
        default="",
        index=True,
    )
    request_body_hash: Mapped[str] = mapped_column(String(64), default="")
    send_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    result_ciphertext: Mapped[str] = mapped_column(Text, default="")
    result_hash: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    action_expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentPendingSelection(Base):
    __tablename__ = "agent_pending_selections"
    __table_args__ = (
        Index(
            "uq_agent_pending_selections_active_patient",
            "patient_id_hash",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING', 'RESOLVED')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
    )
    patient_id_hash: Mapped[str] = mapped_column(
        String(64),
        index=True,
    )
    origin_message_id: Mapped[str] = mapped_column(
        String(160),
        unique=True,
        index=True,
    )
    source_chat_request_id: Mapped[str] = mapped_column(
        String(160),
        index=True,
    )
    trace_id: Mapped[str] = mapped_column(
        String(160),
        index=True,
    )
    selection_type: Mapped[str] = mapped_column(
        String(60),
        index=True,
    )
    payload_ciphertext: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    encryption_key_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        String(32),
        index=True,
        default="PENDING",
    )
    selected_value_hash: Mapped[str] = mapped_column(
        String(64),
        default="",
    )
    resolved_by_message_id: Mapped[str] = mapped_column(
        String(160),
        default="",
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    selection_expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        index=True,
    )


class AgentFeedbackLink(Base):
    __tablename__ = "agent_feedback_links"
    __table_args__ = (
        UniqueConstraint("api_path", "request_id", name="uq_agent_feedback_links_path_request"),
        Index(
            "ix_agent_feedback_links_message_patient",
            "message_id",
            "patient_id_hash",
        ),
        Index(
            "uq_agent_feedback_links_current_reaction",
            "api_path",
            "message_id",
            "patient_id_hash",
            unique=True,
            postgresql_where=text("feedback IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_path: Mapped[str] = mapped_column(String(160))
    request_id: Mapped[str] = mapped_column(String(160), index=True)
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    message_id: Mapped[str] = mapped_column(String(160), index=True)
    patient_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    # Current mutually exclusive reaction state: True=like, False=dislike.
    feedback: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feedback_text_ciphertext: Mapped[str] = mapped_column(Text, default="")
    feedback_text_hash: Mapped[str] = mapped_column(String(64), default="")
    encryption_key_id: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(32), index=True, default="ACCEPTED")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    response_status: Mapped[int] = mapped_column(Integer, default=202)
    response_json: Mapped[str] = mapped_column(Text, default='{"status":"accepted"}')
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    feedback_at: Mapped[datetime] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentProCtcaeSurvey(Base):
    __tablename__ = "agent_pro_ctcae_surveys"
    __table_args__ = (
        Index(
            "uq_agent_pro_ctcae_surveys_active_patient",
            "patient_id_hash",
            unique=True,
            postgresql_where=text(
                "status IN "
                "('AWAITING_RESPONSE', 'COMPLETED', 'APPROVAL_PENDING')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    patient_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    origin_message_id: Mapped[str] = mapped_column(
        String(160),
        unique=True,
        index=True,
    )
    expected_origin_message_id: Mapped[str] = mapped_column(String(160))
    trace_id: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(
        String(32),
        index=True,
        default="AWAITING_RESPONSE",
    )
    question_count: Mapped[int] = mapped_column(Integer)
    current_question_index: Mapped[int] = mapped_column(Integer, default=0)
    payload_ciphertext: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    encryption_key_id: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    response_expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AgentProCtcaeResponse(Base):
    __tablename__ = "agent_pro_ctcae_responses"
    __table_args__ = (
        UniqueConstraint(
            "survey_id",
            "item_code",
            name="uq_agent_pro_ctcae_responses_survey_item",
        ),
        UniqueConstraint(
            "source_user_message_id",
            name="uq_agent_pro_ctcae_responses_source_message",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    survey_id: Mapped[int] = mapped_column(
        ForeignKey(
            "agent_pro_ctcae_surveys.id",
            ondelete="CASCADE",
        ),
        index=True,
    )
    item_code: Mapped[str] = mapped_column(String(80))
    response_index: Mapped[int] = mapped_column(Integer)
    source_user_message_id: Mapped[str] = mapped_column(
        String(160),
        index=True,
    )
    answered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
