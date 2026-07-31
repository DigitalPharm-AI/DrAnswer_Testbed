from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

ENV_FILE = Path("/etc/dranswer-agent-contract/contract.env")
RELEASE_ENV_FILE = Path("/etc/dranswer-agent-contract/release.env")
CURRENT_LINK = Path("/opt/dranswer-agent-contract/current")
UNITS = (
    "dranswer-agent-contract-api.service",
    "dranswer-agent-contract-callback.service",
    "chat-server.service",
)
SYSTEMD_PROPERTIES = (
    "Id",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "MainPID",
    "InvocationID",
    "ExecMainStartTimestamp",
    "NRestarts",
    "Result",
)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def service_snapshot(unit: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            f"--property={','.join(SYSTEMD_PROPERTIES)}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    values: dict[str, Any] = {"Id": unit}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    values["show_exit_code"] = result.returncode
    return values


def contract_database_url(env: dict[str, str]) -> str:
    database_url = env.get("CONTRACT_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("contract_database_configuration_missing")
    if not database_url.startswith(
        ("postgresql://", "postgresql+psycopg://")
    ):
        raise RuntimeError("contract_database_postgresql_required")
    return database_url


def postgresql_snapshot(database_url: str) -> dict[str, Any]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = current_schema()
                          AND table_name IN (
                              'contract_schema_version',
                              'request_records',
                              'callback_jobs'
                          )
                        ORDER BY table_name
                        """
                    )
                ).all()
            ]
            requests = int(
                connection.execute(
                    text("SELECT COUNT(*) FROM request_records")
                ).scalar_one()
            )
            callbacks = int(
                connection.execute(
                    text("SELECT COUNT(*) FROM callback_jobs")
                ).scalar_one()
            )
            callback_statuses = {
                str(status): int(count)
                for status, count in connection.execute(
                    text(
                        """
                        SELECT status, COUNT(*)
                        FROM callback_jobs
                        GROUP BY status
                        ORDER BY status
                        """
                    )
                ).all()
            }
            callback_kinds = {
                str(kind): int(count)
                for kind, count in connection.execute(
                    text(
                        """
                        SELECT callback_kind, COUNT(*)
                        FROM callback_jobs
                        GROUP BY callback_kind
                        ORDER BY callback_kind
                        """
                    )
                ).all()
            }
            schema_version = connection.execute(
                text(
                    """
                    SELECT version
                    FROM contract_schema_version
                    WHERE singleton = TRUE
                    """
                )
            ).scalar_one()
    finally:
        engine.dispose()
    return {
        "kind": "postgresql",
        "schema_version": int(schema_version),
        "tables": tables,
        "request_records": requests,
        "callback_jobs": callbacks,
        "callback_statuses": callback_statuses,
        "callback_kinds": callback_kinds,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    env = {
        **read_env(ENV_FILE),
        **read_env(RELEASE_ENV_FILE),
    }
    release = env["APP_RELEASE_VERSION"]
    archive = Path(
        f"/home/ec2-user/dranswer-agent-contract-{release}.tar.gz"
    )
    archive_sha256 = (
        hashlib.sha256(archive.read_bytes()).hexdigest()
        if archive.is_file()
        else None
    )
    database = postgresql_snapshot(contract_database_url(env))

    socket_result = subprocess.run(
        ["ss", "-ltnp"],
        check=False,
        capture_output=True,
        text=True,
    )
    listeners = [
        line.strip()
        for line in socket_result.stdout.splitlines()
        if ":8701" in line or ":8766" in line
    ]
    document = {
        "schema_version": "1",
        "label": args.label,
        "captured_at": datetime.now(UTC).isoformat(),
        "release": release,
        "current_target": str(CURRENT_LINK.resolve()),
        "archive_sha256": archive_sha256,
        "services": {
            unit: service_snapshot(unit)
            for unit in UNITS
        },
        "listeners": listeners,
        "database": database,
        "callback_mode": env["CALLBACK_MODE"],
        "backend_callback_configured": bool(
            env.get("BACKEND_CALLBACK_BASE_URL")
            and env.get("BACKEND_API_TOKEN")
        ),
    }
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "label": args.label,
                "release": release,
                "database_kind": database["kind"],
                "request_records": database["request_records"],
                "callback_jobs": database["callback_jobs"],
                "output": str(args.output),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
