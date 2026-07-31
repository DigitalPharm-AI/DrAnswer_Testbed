from __future__ import annotations

import json

from shared.db import (
    DatabaseEngineConfig,
    create_database_engine,
    database_server_identity,
)
from shared.settings import get_settings
from system_app.schema import migrate_system_schema


def main() -> int:
    settings = get_settings()
    database_url = settings.system_migration_database_url.strip()
    if not database_url:
        raise RuntimeError(
            "SYSTEM_MIGRATION_DATABASE_URL is required."
        )
    engine = create_database_engine(
        database_url,
        config=DatabaseEngineConfig(
            pool_size=1,
            max_overflow=0,
            pool_timeout_seconds=settings.system_db_pool_timeout_seconds,
            pool_recycle_seconds=settings.system_db_pool_recycle_seconds,
            statement_timeout_ms=0,
            lock_timeout_ms=0,
        ),
    )
    try:
        result = migrate_system_schema(engine)
        identity = database_server_identity(engine)
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "ok": True,
                **identity,
                "latest_migration": result["latest_version"],
                "applied_count": len(result["applied_now"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
