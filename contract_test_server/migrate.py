from __future__ import annotations

import logging

from contract_test_server.config import get_contract_server_settings
from contract_test_server.storage import ContractStore


def main() -> int:
    settings = get_contract_server_settings()
    settings.require_contract_migration_postgresql()
    store = ContractStore(
        settings.contract_migration_database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout_seconds=(
            settings.contract_db_pool_timeout_seconds
        ),
        statement_timeout_ms=(
            settings.contract_db_statement_timeout_ms
        ),
        lock_timeout_ms=settings.contract_db_lock_timeout_ms,
    )
    try:
        store.initialize()
        if not store.ready():
            raise RuntimeError(
                "contract PostgreSQL schema verification failed"
            )
    finally:
        store.dispose()
    logging.basicConfig(
        level=getattr(
            logging,
            settings.log_level.upper(),
            logging.INFO,
        )
    )
    logging.getLogger(__name__).info(
        "contract_test_server schema migration completed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
