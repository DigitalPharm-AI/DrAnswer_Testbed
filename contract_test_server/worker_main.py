from __future__ import annotations

import argparse
import logging
import signal
from threading import Event

from contract_test_server.callbacks import CallbackDispatcher
from contract_test_server.config import get_contract_server_settings
from contract_test_server.storage import ContractStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deliver explicitly released v1.3 callback jobs.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Attempt at most one due callback and exit.",
    )
    args = parser.parse_args()

    settings = get_contract_server_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if settings.callback_mode != "deliver":
        logging.getLogger(__name__).info(
            "callback_worker status=disabled mode=%s",
            settings.callback_mode,
        )
        return 0
    if not settings.callback_delivery_configured:
        logging.getLogger(__name__).error(
            "callback_worker status=not_ready reason=delivery_config"
        )
        return 2

    settings.require_contract_postgresql()
    store = ContractStore(
        settings.contract_database_url,
        pool_size=settings.contract_db_pool_size,
        max_overflow=settings.contract_db_max_overflow,
        pool_timeout_seconds=settings.contract_db_pool_timeout_seconds,
        statement_timeout_ms=settings.contract_db_statement_timeout_ms,
        lock_timeout_ms=settings.contract_db_lock_timeout_ms,
    )
    try:
        store.recover_expired_in_progress()
        dispatcher = CallbackDispatcher(settings=settings, store=store)
        if args.once:
            dispatcher.deliver_one()
            return 0

        stop = Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        while not stop.is_set():
            result = dispatcher.deliver_one()
            if not result.delivered:
                stop.wait(settings.callback_poll_seconds)
        return 0
    finally:
        store.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
