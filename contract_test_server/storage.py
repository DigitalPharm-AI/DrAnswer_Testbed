from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Literal
from uuid import uuid4

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from shared.db import DatabaseEngineConfig, create_database_engine

CALLBACK_PATHS = frozenset(
    {
        "/api/agent/async/missed-dose-results",
        "/api/agent/async/notification-policy-change-proposals",
    }
)
CallbackStatus = Literal[
    "held",
    "pending",
    "in_progress",
    "retry_wait",
    "completed",
    "dead",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


def from_db_datetime(
    value: datetime | str | None,
) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_request_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def keyed_request_hash(
    value: Any,
    *,
    secret: str,
    domain: str,
) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        domain.encode("utf-8") + b"\0" + canonical_json_bytes(value),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class CallbackInsert:
    callback_request_id: str
    source_request_id: str
    callback_kind: str
    callback_path: str
    payload: dict[str, Any]
    initial_status: Literal["held", "pending"]


@dataclass(frozen=True)
class StoredRequest:
    outcome: Literal["new", "replay", "conflict"]
    response_status: int | None = None
    response_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClaimedCallback:
    callback_request_id: str
    callback_kind: str
    callback_path: str
    payload_bytes: bytes
    attempt: int
    claim_id: str


class ContractStore:
    """PostgreSQL-only request-idempotency and callback-outbox store."""

    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout_seconds: float = 30.0,
        statement_timeout_ms: int = 30_000,
        lock_timeout_ms: int = 5_000,
    ) -> None:
        self.engine: Engine = create_database_engine(
            database_url,
            config=DatabaseEngineConfig(
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout_seconds=pool_timeout_seconds,
                statement_timeout_ms=statement_timeout_ms,
                lock_timeout_ms=lock_timeout_ms,
            ),
        )

    def initialize(self) -> None:
        """Create or upgrade the dedicated contract-server schema.

        Runtime services do not call this method. It is reserved for the
        explicit ``python -m contract_test_server.migrate`` deployment step
        and isolated PostgreSQL tests.
        """

        statements = (
            """
            CREATE TABLE IF NOT EXISTS contract_schema_version (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE
                    CHECK (singleton),
                version INTEGER NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS request_records (
                api_path TEXT NOT NULL,
                request_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                response_json TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (api_path, request_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS callback_jobs (
                callback_request_id TEXT PRIMARY KEY,
                source_request_id TEXT NOT NULL,
                callback_kind TEXT NOT NULL,
                callback_path TEXT NOT NULL,
                payload_json BYTEA NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'held',
                        'pending',
                        'in_progress',
                        'retry_wait',
                        'completed',
                        'dead'
                    )
                ),
                attempts INTEGER NOT NULL DEFAULT 0
                    CHECK (attempts >= 0),
                claim_id TEXT,
                lease_expires_at TIMESTAMPTZ,
                next_attempt_at TIMESTAMPTZ,
                last_http_status INTEGER,
                last_error_code TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_callback_jobs_due
            ON callback_jobs (status, next_attempt_at, created_at)
            """,
        )
        with self.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
            connection.execute(
                text(
                    """
                    INSERT INTO contract_schema_version (
                        singleton,
                        version,
                        updated_at
                    ) VALUES (TRUE, 1, :updated_at)
                    ON CONFLICT (singleton) DO UPDATE
                    SET version = EXCLUDED.version,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {"updated_at": utc_now()},
            )

    def dispose(self) -> None:
        self.engine.dispose()

    def recover_expired_in_progress(self) -> int:
        now = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE callback_jobs
                    SET status = 'retry_wait',
                        next_attempt_at = :now,
                        claim_id = NULL,
                        lease_expires_at = NULL,
                        last_error_code =
                            'worker_restarted_during_delivery',
                        updated_at = :now
                    WHERE status = 'in_progress'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= :now
                    """
                ),
                {"now": now},
            )
            return int(result.rowcount)

    def ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT
                            to_regclass('request_records') IS NOT NULL
                                AS requests_ready,
                            to_regclass('callback_jobs') IS NOT NULL
                                AS callbacks_ready,
                            (
                                SELECT version
                                FROM contract_schema_version
                                WHERE singleton = TRUE
                            ) AS schema_version
                        """
                    )
                ).mappings().one()
            return bool(
                row["requests_ready"]
                and row["callbacks_ready"]
                and row["schema_version"] == 1
            )
        except SQLAlchemyError:
            return False

    def register_request(
        self,
        *,
        api_path: str,
        request_id: str,
        request_hash: str,
        response_status: int,
        response_json: dict[str, Any],
        callbacks: Iterable[CallbackInsert] = (),
    ) -> StoredRequest:
        now = utc_now()
        response_text = canonical_json_bytes(response_json).decode("utf-8")
        prepared_callbacks: list[tuple[CallbackInsert, bytes]] = []
        for callback in callbacks:
            if callback.callback_path not in CALLBACK_PATHS:
                raise ValueError("callback_path_not_allowlisted")
            prepared_callbacks.append(
                (callback, canonical_json_bytes(callback.payload))
            )

        with self.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    """
                    INSERT INTO request_records (
                        api_path,
                        request_id,
                        request_hash,
                        response_status,
                        response_json,
                        created_at
                    ) VALUES (
                        :api_path,
                        :request_id,
                        :request_hash,
                        :response_status,
                        :response_json,
                        :created_at
                    )
                    ON CONFLICT (api_path, request_id) DO NOTHING
                    RETURNING request_id
                    """
                ),
                {
                    "api_path": api_path,
                    "request_id": request_id,
                    "request_hash": request_hash,
                    "response_status": response_status,
                    "response_json": response_text,
                    "created_at": now,
                },
            ).first()
            if inserted is None:
                existing = connection.execute(
                    text(
                        """
                        SELECT
                            request_hash,
                            response_status,
                            response_json
                        FROM request_records
                        WHERE api_path = :api_path
                          AND request_id = :request_id
                        """
                    ),
                    {
                        "api_path": api_path,
                        "request_id": request_id,
                    },
                ).mappings().one()
                if existing["request_hash"] != request_hash:
                    return StoredRequest(outcome="conflict")
                return StoredRequest(
                    outcome="replay",
                    response_status=int(existing["response_status"]),
                    response_json=json.loads(existing["response_json"]),
                )

            for callback, payload_bytes in prepared_callbacks:
                connection.execute(
                    text(
                        """
                        INSERT INTO callback_jobs (
                            callback_request_id,
                            source_request_id,
                            callback_kind,
                            callback_path,
                            payload_json,
                            payload_hash,
                            status,
                            attempts,
                            next_attempt_at,
                            created_at,
                            updated_at
                        ) VALUES (
                            :callback_request_id,
                            :source_request_id,
                            :callback_kind,
                            :callback_path,
                            :payload_json,
                            :payload_hash,
                            :status,
                            0,
                            :next_attempt_at,
                            :created_at,
                            :updated_at
                        )
                        """
                    ),
                    {
                        "callback_request_id": callback.callback_request_id,
                        "source_request_id": callback.source_request_id,
                        "callback_kind": callback.callback_kind,
                        "callback_path": callback.callback_path,
                        "payload_json": payload_bytes,
                        "payload_hash": hashlib.sha256(
                            payload_bytes
                        ).hexdigest(),
                        "status": callback.initial_status,
                        "next_attempt_at": (
                            now
                            if callback.initial_status == "pending"
                            else None
                        ),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
        return StoredRequest(
            outcome="new",
            response_status=response_status,
            response_json=response_json,
        )

    def list_callbacks(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT
                        callback_request_id,
                        source_request_id,
                        callback_kind,
                        callback_path,
                        status,
                        attempts,
                        next_attempt_at,
                        last_http_status,
                        last_error_code,
                        payload_json,
                        created_at,
                        updated_at
                    FROM callback_jobs
                    ORDER BY created_at DESC, callback_request_id ASC
                    LIMIT :limit
                    """
                ),
                {"limit": max(1, min(limit, 500))},
            ).mappings().all()
        return [
            {
                "callback_request_id": row["callback_request_id"],
                "source_request_id": row["source_request_id"],
                "callback_kind": row["callback_kind"],
                "callback_path": row["callback_path"],
                "status": row["status"],
                "attempts": row["attempts"],
                "next_attempt_at": from_db_datetime(
                    row["next_attempt_at"]
                ),
                "last_http_status": row["last_http_status"],
                "last_error_code": row["last_error_code"],
                "payload": json.loads(bytes(row["payload_json"])),
                "created_at": from_db_datetime(row["created_at"]),
                "updated_at": from_db_datetime(row["updated_at"]),
            }
            for row in rows
        ]

    def release_callback(self, callback_request_id: str) -> Literal[
        "released",
        "already_released",
        "not_found",
    ]:
        now = utc_now()
        with self.engine.begin() as connection:
            released = connection.execute(
                text(
                    """
                    UPDATE callback_jobs
                    SET status = 'pending',
                        next_attempt_at = :now,
                        last_error_code = NULL,
                        updated_at = :now
                    WHERE callback_request_id = :callback_request_id
                      AND status = 'held'
                    RETURNING callback_request_id
                    """
                ),
                {
                    "now": now,
                    "callback_request_id": callback_request_id,
                },
            ).first()
            if released is not None:
                return "released"
            status = connection.execute(
                text(
                    """
                    SELECT status
                    FROM callback_jobs
                    WHERE callback_request_id = :callback_request_id
                    """
                ),
                {"callback_request_id": callback_request_id},
            ).scalar_one_or_none()
            if status is None:
                return "not_found"
            return "already_released"

    def claim_due_callback(
        self,
        *,
        lease_seconds: float = 60,
        max_attempts: int = 2_147_483_647,
    ) -> ClaimedCallback | None:
        current_time = utc_now()
        claim_id = uuid4().hex
        lease_expires_at = current_time + timedelta(
            seconds=max(1, lease_seconds)
        )
        attempt_limit = max(1, max_attempts)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE callback_jobs
                    SET status = 'retry_wait',
                        next_attempt_at = :now,
                        claim_id = NULL,
                        lease_expires_at = NULL,
                        last_error_code = 'callback_lease_expired',
                        updated_at = :now
                    WHERE status = 'in_progress'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= :now
                    """
                ),
                {"now": current_time},
            )
            connection.execute(
                text(
                    """
                    UPDATE callback_jobs
                    SET status = 'dead',
                        next_attempt_at = NULL,
                        claim_id = NULL,
                        lease_expires_at = NULL,
                        last_error_code =
                            'retry_exhausted:callback_lease_expired',
                        updated_at = :now
                    WHERE status IN ('pending', 'retry_wait')
                      AND attempts >= :max_attempts
                    """
                ),
                {
                    "now": current_time,
                    "max_attempts": attempt_limit,
                },
            )
            row = connection.execute(
                text(
                    """
                    SELECT
                        callback_request_id,
                        callback_kind,
                        callback_path,
                        payload_json,
                        attempts
                    FROM callback_jobs
                    WHERE status IN ('pending', 'retry_wait')
                      AND attempts < :max_attempts
                      AND (
                          next_attempt_at IS NULL
                          OR next_attempt_at <= :now
                      )
                    ORDER BY created_at ASC, callback_request_id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                ),
                {
                    "max_attempts": attempt_limit,
                    "now": current_time,
                },
            ).mappings().first()
            if row is None:
                return None
            attempt = int(row["attempts"]) + 1
            connection.execute(
                text(
                    """
                    UPDATE callback_jobs
                    SET status = 'in_progress',
                        attempts = :attempt,
                        claim_id = :claim_id,
                        lease_expires_at = :lease_expires_at,
                        updated_at = :updated_at
                    WHERE callback_request_id = :callback_request_id
                    """
                ),
                {
                    "attempt": attempt,
                    "claim_id": claim_id,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": current_time,
                    "callback_request_id": row[
                        "callback_request_id"
                    ],
                },
            )
        return ClaimedCallback(
            callback_request_id=row["callback_request_id"],
            callback_kind=row["callback_kind"],
            callback_path=row["callback_path"],
            payload_bytes=bytes(row["payload_json"]),
            attempt=attempt,
            claim_id=claim_id,
        )

    def mark_callback_completed(
        self,
        callback_request_id: str,
        *,
        claim_id: str,
        http_status: int,
    ) -> bool:
        return self._finish_callback(
            callback_request_id,
            claim_id=claim_id,
            status="completed",
            http_status=http_status,
            error_code=None,
            next_attempt_at=None,
        )

    def mark_callback_dead(
        self,
        callback_request_id: str,
        *,
        claim_id: str,
        http_status: int | None,
        error_code: str,
    ) -> bool:
        return self._finish_callback(
            callback_request_id,
            claim_id=claim_id,
            status="dead",
            http_status=http_status,
            error_code=error_code,
            next_attempt_at=None,
        )

    def mark_callback_retry(
        self,
        callback_request_id: str,
        *,
        claim_id: str,
        http_status: int | None,
        error_code: str,
        delay_seconds: float,
    ) -> bool:
        return self._finish_callback(
            callback_request_id,
            claim_id=claim_id,
            status="retry_wait",
            http_status=http_status,
            error_code=error_code,
            next_attempt_at=utc_now()
            + timedelta(seconds=delay_seconds),
        )

    def _finish_callback(
        self,
        callback_request_id: str,
        *,
        claim_id: str,
        status: Literal["completed", "dead", "retry_wait"],
        http_status: int | None,
        error_code: str | None,
        next_attempt_at: datetime | None,
    ) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE callback_jobs
                    SET status = :status,
                        next_attempt_at = :next_attempt_at,
                        claim_id = NULL,
                        lease_expires_at = NULL,
                        last_http_status = :http_status,
                        last_error_code = :error_code,
                        updated_at = :updated_at
                    WHERE callback_request_id = :callback_request_id
                      AND status = 'in_progress'
                      AND claim_id = :claim_id
                    """
                ),
                {
                    "status": status,
                    "next_attempt_at": next_attempt_at,
                    "http_status": http_status,
                    "error_code": error_code,
                    "updated_at": utc_now(),
                    "callback_request_id": callback_request_id,
                    "claim_id": claim_id,
                },
            )
            return result.rowcount == 1
