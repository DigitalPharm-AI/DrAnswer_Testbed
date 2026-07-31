from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

REQUEST_COLUMNS = (
    "api_path",
    "request_id",
    "request_hash",
    "response_status",
    "response_json",
    "created_at",
)
CALLBACK_COLUMNS = (
    "callback_request_id",
    "source_request_id",
    "callback_kind",
    "callback_path",
    "payload_json",
    "payload_hash",
    "status",
    "attempts",
    "claim_id",
    "lease_expires_at",
    "next_attempt_at",
    "last_http_status",
    "last_error_code",
    "created_at",
    "updated_at",
)
DATETIME_COLUMNS = {
    "created_at",
    "updated_at",
    "lease_expires_at",
    "next_attempt_at",
}
EXPECTED_TABLES = {
    "contract_schema_version",
    "request_records",
    "callback_jobs",
}
ALLOWED_CALLBACK_STATUSES = {
    "held",
    "pending",
    "in_progress",
    "retry_wait",
    "completed",
    "dead",
}


def parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"unsupported datetime type: {type(value).__name__}")
    if parsed is None:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive datetime in SQLite source")
    return parsed.astimezone(UTC)


def normalize_digest_value(column: str, value: Any) -> Any:
    if column in DATETIME_COLUMNS:
        parsed = parse_datetime(value)
        return (
            parsed.isoformat(timespec="microseconds")
            if parsed is not None
            else None
        )
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    return value


def rows_digest(
    rows: Iterable[Mapping[str, Any]],
    columns: Sequence[str],
) -> str:
    digest = hashlib.sha256()
    for row in rows:
        normalized = [
            normalize_digest_value(column, row[column])
            for column in columns
        ]
        digest.update(
            json.dumps(
                normalized,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def sqlite_integrity(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise RuntimeError("sqlite_integrity_check_failed")


def sqlite_rows(
    connection: sqlite3.Connection,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    requests = connection.execute(
        f"""
        SELECT {", ".join(REQUEST_COLUMNS)}
        FROM request_records
        ORDER BY api_path, request_id
        """
    ).fetchall()
    callbacks = connection.execute(
        f"""
        SELECT {", ".join(CALLBACK_COLUMNS)}
        FROM callback_jobs
        ORDER BY callback_request_id
        """
    ).fetchall()
    return requests, callbacks


def validate_sqlite_state(
    requests: Sequence[Mapping[str, Any]],
    callbacks: Sequence[Mapping[str, Any]],
) -> None:
    for row in requests:
        parse_datetime(row["created_at"])
        json.loads(str(row["response_json"]))
    for row in callbacks:
        status = str(row["status"])
        if status not in ALLOWED_CALLBACK_STATUSES:
            raise RuntimeError(f"invalid_callback_status:{status}")
        if status == "in_progress":
            raise RuntimeError("in_progress_callback_blocks_cutover")
        payload = bytes(row["payload_json"])
        if hashlib.sha256(payload).hexdigest() != row["payload_hash"]:
            raise RuntimeError("callback_payload_hash_mismatch")
        json.loads(payload)
        for column in DATETIME_COLUMNS:
            if column in row.keys():
                parse_datetime(row[column])


def state_summary(
    requests: Sequence[Mapping[str, Any]],
    callbacks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "request_records": len(requests),
        "callback_jobs": len(callbacks),
        "callback_statuses": dict(
            sorted(
                Counter(str(row["status"]) for row in callbacks).items()
            )
        ),
        "request_digest": rows_digest(requests, REQUEST_COLUMNS),
        "callback_digest": rows_digest(callbacks, CALLBACK_COLUMNS),
    }


def create_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise FileExistsError(f"snapshot already exists: {destination}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open_sqlite_readonly(source) as source_connection:
        sqlite_integrity(source_connection)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    os.chmod(destination, 0o600)
    with open_sqlite_readonly(destination) as snapshot_connection:
        sqlite_integrity(snapshot_connection)
        requests, callbacks = sqlite_rows(snapshot_connection)
    validate_sqlite_state(requests, callbacks)
    return {
        "operation": "snapshot",
        "snapshot": str(destination),
        "snapshot_sha256": hashlib.sha256(
            destination.read_bytes()
        ).hexdigest(),
        **state_summary(requests, callbacks),
    }


def require_postgresql_psycopg_url(value: str, variable: str) -> str:
    database_url = value.strip()
    if not database_url:
        raise RuntimeError(f"{variable}_missing")
    if not database_url.startswith("postgresql+psycopg://"):
        raise RuntimeError(f"{variable}_must_use_postgresql_psycopg")
    return database_url


def engine_for(database_url: str) -> Engine:
    return create_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=10,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=30000 -c lock_timeout=5000",
        },
    )


def postgres_schema_gate(connection: Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                """
            )
        ).all()
    }
    if not EXPECTED_TABLES.issubset(tables):
        raise RuntimeError("postgres_schema_tables_missing")
    version = connection.execute(
        text(
            """
            SELECT version
            FROM contract_schema_version
            WHERE singleton = TRUE
            """
        )
    ).scalar_one()
    if int(version) != 1:
        raise RuntimeError("postgres_schema_version_mismatch")


def postgres_rows(
    connection: Connection,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    requests = connection.execute(
        text(
            f"""
            SELECT {", ".join(REQUEST_COLUMNS)}
            FROM request_records
            ORDER BY api_path, request_id
            """
        )
    ).mappings().all()
    callbacks = connection.execute(
        text(
            f"""
            SELECT {", ".join(CALLBACK_COLUMNS)}
            FROM callback_jobs
            ORDER BY callback_request_id
            """
        )
    ).mappings().all()
    return list(requests), list(callbacks)


def request_parameters(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        column: (
            parse_datetime(row[column])
            if column in DATETIME_COLUMNS
            else row[column]
        )
        for column in REQUEST_COLUMNS
    }


def callback_parameters(row: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for column in CALLBACK_COLUMNS:
        value = row[column]
        if column in DATETIME_COLUMNS:
            value = parse_datetime(value)
        elif column == "payload_json":
            value = bytes(value)
        values[column] = value
    return values


def import_snapshot(snapshot: Path, database_url: str) -> dict[str, Any]:
    with open_sqlite_readonly(snapshot) as sqlite_connection:
        sqlite_integrity(sqlite_connection)
        source_requests, source_callbacks = sqlite_rows(sqlite_connection)
    validate_sqlite_state(source_requests, source_callbacks)
    source_summary = state_summary(source_requests, source_callbacks)

    engine = engine_for(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtext('dranswer-contract-sqlite-cutover-v1')
                    )
                    """
                )
            )
            postgres_schema_gate(connection)
            target_requests, target_callbacks = postgres_rows(connection)
            if target_requests or target_callbacks:
                raise RuntimeError("postgres_target_is_not_empty")

            if source_requests:
                connection.execute(
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
                        """
                    ),
                    [
                        request_parameters(row)
                        for row in source_requests
                    ],
                )
            if source_callbacks:
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
                            claim_id,
                            lease_expires_at,
                            next_attempt_at,
                            last_http_status,
                            last_error_code,
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
                            :attempts,
                            :claim_id,
                            :lease_expires_at,
                            :next_attempt_at,
                            :last_http_status,
                            :last_error_code,
                            :created_at,
                            :updated_at
                        )
                        """
                    ),
                    [
                        callback_parameters(row)
                        for row in source_callbacks
                    ],
                )

            imported_requests, imported_callbacks = postgres_rows(connection)
            target_summary = state_summary(
                imported_requests,
                imported_callbacks,
            )
            if target_summary != source_summary:
                raise RuntimeError("postgres_import_parity_failed")
    finally:
        engine.dispose()
    return {
        "operation": "import",
        "source": str(snapshot),
        **source_summary,
        "parity": True,
    }


def verify_runtime_privileges(
    runtime_url: str,
    migration_url: str,
) -> dict[str, Any]:
    runtime_engine = engine_for(runtime_url)
    migration_engine = engine_for(migration_url)
    canary_request_id = f"req_privilege_canary_{secrets.token_hex(8)}"
    try:
        with migration_engine.connect() as connection:
            migration_identity = connection.execute(
                text("SELECT current_user, current_database()")
            ).one()
            migration_can_create = bool(
                connection.execute(
                    text(
                        """
                        SELECT has_schema_privilege(
                            current_user,
                            current_schema(),
                            'CREATE'
                        )
                        """
                    )
                ).scalar_one()
            )
            if not migration_can_create:
                raise RuntimeError("migration_schema_create_missing")
        with runtime_engine.connect() as connection:
            runtime_identity = connection.execute(
                text("SELECT current_user, current_database()")
            ).one()
            postgres_schema_gate(connection)
            privileges = connection.execute(
                text(
                    """
                    SELECT
                        has_schema_privilege(
                            current_user,
                            current_schema(),
                            'USAGE'
                        ) AS schema_usage,
                        has_schema_privilege(
                            current_user,
                            current_schema(),
                            'CREATE'
                        ) AS schema_create,
                        has_table_privilege(
                            current_user,
                            'request_records',
                            'SELECT,INSERT,UPDATE'
                        ) AS request_dml,
                        has_table_privilege(
                            current_user,
                            'callback_jobs',
                            'SELECT,INSERT,UPDATE'
                        ) AS callback_dml
                    """
                )
            ).mappings().one()
            if not (
                privileges["schema_usage"]
                and privileges["request_dml"]
                and privileges["callback_dml"]
            ):
                raise RuntimeError("runtime_dml_privileges_missing")
            if privileges["schema_create"]:
                raise RuntimeError("runtime_schema_create_not_denied")

            connection.rollback()
            transaction = connection.begin()
            try:
                connection.execute(
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
                            '/_cutover/privilege-canary',
                            :request_id,
                            :request_hash,
                            202,
                            '{"status":"canary"}',
                            :created_at
                        )
                        """
                    ),
                    {
                        "request_id": canary_request_id,
                        "request_hash": hashlib.sha256(
                            canary_request_id.encode("utf-8")
                        ).hexdigest(),
                        "created_at": datetime.now(UTC),
                    },
                )
                connection.execute(
                    text(
                        """
                        UPDATE request_records
                        SET response_status = 200
                        WHERE api_path = '/_cutover/privilege-canary'
                          AND request_id = :request_id
                        """
                    ),
                    {"request_id": canary_request_id},
                )
                status = connection.execute(
                    text(
                        """
                        SELECT response_status
                        FROM request_records
                        WHERE api_path = '/_cutover/privilege-canary'
                          AND request_id = :request_id
                        """
                    ),
                    {"request_id": canary_request_id},
                ).scalar_one()
                if int(status) != 200:
                    raise RuntimeError("runtime_dml_canary_failed")
            finally:
                transaction.rollback()

            create_transaction = connection.begin()
            create_denied = False
            try:
                connection.execute(
                    text(
                        "CREATE TABLE contract_runtime_ddl_canary (id INT)"
                    )
                )
            except DBAPIError as exc:
                create_denied = getattr(
                    getattr(exc, "orig", None),
                    "sqlstate",
                    None,
                ) == "42501"
            finally:
                create_transaction.rollback()
            if not create_denied:
                raise RuntimeError("runtime_create_table_not_denied")
    finally:
        runtime_engine.dispose()
        migration_engine.dispose()

    if runtime_identity[0] == migration_identity[0]:
        raise RuntimeError("runtime_and_migration_roles_not_separated")
    if runtime_identity[1] != migration_identity[1]:
        raise RuntimeError("runtime_and_migration_databases_differ")
    return {
        "operation": "verify_roles",
        "runtime_role": str(runtime_identity[0]),
        "migration_role": str(migration_identity[0]),
        "database": str(runtime_identity[1]),
        "runtime_dml_canary": "rolled_back",
        "runtime_create_denied": True,
    }


def write_result(result: dict[str, Any], output: Path | None) -> None:
    document = {
        "schema_version": "1",
        "completed_at": datetime.now(UTC).isoformat(),
        **result,
    }
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        os.chmod(output, 0o600)
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--source", type=Path, required=True)
    snapshot_parser.add_argument("--destination", type=Path, required=True)
    snapshot_parser.add_argument("--output", type=Path)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--source", type=Path, required=True)
    import_parser.add_argument("--output", type=Path)

    roles_parser = subparsers.add_parser("verify-roles")
    roles_parser.add_argument("--output", type=Path)

    args = parser.parse_args()
    if args.operation == "snapshot":
        result = create_snapshot(args.source, args.destination)
    elif args.operation == "import":
        migration_url = require_postgresql_psycopg_url(
            os.environ.get("CONTRACT_MIGRATION_DATABASE_URL", ""),
            "CONTRACT_MIGRATION_DATABASE_URL",
        )
        result = import_snapshot(args.source, migration_url)
    else:
        runtime_url = require_postgresql_psycopg_url(
            os.environ.get("CONTRACT_DATABASE_URL", ""),
            "CONTRACT_DATABASE_URL",
        )
        migration_url = require_postgresql_psycopg_url(
            os.environ.get("CONTRACT_MIGRATION_DATABASE_URL", ""),
            "CONTRACT_MIGRATION_DATABASE_URL",
        )
        result = verify_runtime_privileges(runtime_url, migration_url)
    write_result(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
