from __future__ import annotations

import logging
import signal
import threading

from sqlalchemy.orm import Session

from agent_app.async_tasks import reset_running_async_tasks
from agent_app.async_worker import async_task_worker
from agent_app.db import engine
from agent_app.migrations import run_migrations
from agent_app.models import Base
from agent_app.runtime import create_orchestrator
from shared.settings import get_settings

logger = logging.getLogger("agent_app.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    settings.require_internal_api_token_in_production()
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    with Session(engine) as session:
        restored = reset_running_async_tasks(session)
        session.commit()
    if restored:
        logger.info("restored_running_async_tasks count=%s", restored)

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("agent_worker_shutdown_requested signal=%s", signum)
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_stop)

    logger.info("agent_async_worker_starting")
    async_task_worker(stop_event, create_orchestrator())
    logger.info("agent_async_worker_stopped")


if __name__ == "__main__":
    main()
