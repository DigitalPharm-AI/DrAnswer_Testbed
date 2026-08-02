from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from agent_app.jobs.tasks import claim_next_async_task
from agent_app.persistence.migrations import required_migration_versions
from agent_app.persistence.models import AgentAsyncTask, AgentRunTrace
from agent_app.persistence.schema import (
    migrate_agent_schema,
    verify_agent_schema_current,
)
from shared.time_utils import utc_now

POSTGRES_URL = os.getenv("AGENT_POSTGRES_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="AGENT_POSTGRES_TEST_DATABASE_URL is not configured",
)


@pytest.fixture
def isolated_agent_postgres():
    schema_name = f"agent_ci_{uuid.uuid4().hex}"
    base_url = make_url(POSTGRES_URL)
    admin_engine = create_engine(base_url, future=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    query = dict(base_url.query)
    query["options"] = f"-csearch_path={schema_name},public"
    target_engine = create_engine(
        base_url.set(query=query),
        pool_pre_ping=True,
        future=True,
    )
    try:
        yield target_engine
    finally:
        target_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                text(f'DROP SCHEMA "{schema_name}" CASCADE')
            )
        admin_engine.dispose()


def test_agent_postgresql_schema_and_multiworker_claim(
    isolated_agent_postgres,
) -> None:
    target_engine = isolated_agent_postgres

    with ThreadPoolExecutor(max_workers=2) as executor:
        migration_results = list(
            executor.map(
                lambda _index: migrate_agent_schema(target_engine),
                range(2),
            )
        )

    assert verify_agent_schema_current(target_engine)["schema_current"] is True
    assert sum(
        len(result["applied_now"])
        for result in migration_results
    ) > 0
    assert any(
        result["latest_version"]
        == required_migration_versions(target_engine)[-1]
        for result in migration_results
    )
    assert migrate_agent_schema(target_engine)["applied_now"] == []

    now = utc_now()
    with Session(target_engine) as session:
        session.add(
            AgentRunTrace(
                trace_id="trace-postgres-copy",
                request_id="request-postgres-copy",
                patient_id_hash="patient-hash",
                workflow_name="postgres-ci",
                status="COMPLETED",
                expires_at=now + timedelta(days=1),
            )
        )
        session.commit()

    with Session(target_engine) as session:
        copied = session.scalar(
            select(AgentRunTrace).where(
                AgentRunTrace.trace_id == "trace-postgres-copy"
            )
        )
        assert copied is not None
        assert session.scalar(
            select(func.count()).select_from(AgentRunTrace)
        ) == 1
        next_trace = AgentRunTrace(
            trace_id="trace-after-sequence-reset",
            request_id="request-after-sequence-reset",
            patient_id_hash="patient-hash",
            workflow_name="postgres-ci",
            status="COMPLETED",
            expires_at=now + timedelta(days=1),
        )
        session.add(next_trace)
        session.flush()
        assert next_trace.id == copied.id + 1

        session.add_all(
            [
                AgentAsyncTask(
                    request_id=f"claim-{index}",
                    task_type="medication_event",
                    status="pending",
                        payload_json="{}",
                        callback_context_json="{}",
                        accepted_at=now,
                        expires_at=now + timedelta(days=1),
                    )
                for index in range(2)
            ]
        )
        session.commit()

    claim_barrier = Barrier(2)

    def claim(worker_id: str) -> str:
        with Session(target_engine) as session:
            with session.begin():
                task = claim_next_async_task(
                    session,
                    worker_id=worker_id,
                    visibility_timeout_seconds=60,
                )
                assert task is not None
                claim_barrier.wait(timeout=10)
                return task.request_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(
            executor.map(claim, ("worker-a", "worker-b"))
        )

    assert set(claimed) == {"claim-0", "claim-1"}


def test_agent_schema_rejects_retired_conversation_storage(
    isolated_agent_postgres,
) -> None:
    migrate_agent_schema(isolated_agent_postgres)
    with isolated_agent_postgres.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE agent_conversation_locks "
                "(id INTEGER PRIMARY KEY)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="agent_database_retired_schema_present",
    ):
        verify_agent_schema_current(isolated_agent_postgres)


def test_agent_schema_rejects_retired_conversation_named_objects(
    isolated_agent_postgres,
) -> None:
    migrate_agent_schema(isolated_agent_postgres)
    with isolated_agent_postgres.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX ix_retired_conversation_id "
                "ON agent_run_traces (patient_id_hash)"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="agent_database_retired_schema_present",
    ):
        verify_agent_schema_current(isolated_agent_postgres)


def test_agent_schema_rejects_retired_fallback_column(
    isolated_agent_postgres,
) -> None:
    migrate_agent_schema(isolated_agent_postgres)
    with isolated_agent_postgres.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE agent_run_traces "
                "ADD COLUMN fallback_reason VARCHAR(160) "
                "NOT NULL DEFAULT ''"
            )
        )

    with pytest.raises(
        RuntimeError,
        match="agent_database_retired_schema_present",
    ):
        verify_agent_schema_current(isolated_agent_postgres)
