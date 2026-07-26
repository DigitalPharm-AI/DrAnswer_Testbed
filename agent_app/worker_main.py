from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from sqlalchemy.orm import Session

from agent_app.jobs.tasks import reset_running_async_tasks
from agent_app.jobs.worker import async_task_worker
from agent_app.persistence.db import engine
from agent_app.persistence.migrations import run_migrations
from agent_app.persistence.models import Base
from agent_app.runtime import create_orchestrator
from shared.settings import get_settings

logger = logging.getLogger("agent_app.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    runtime_pid_path = _write_runtime_pid()
    try:
        _run_worker()
    finally:
        if runtime_pid_path is not None:
            runtime_pid_path.unlink(missing_ok=True)


def _run_worker() -> None:
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


def _write_runtime_pid() -> Path | None:
    raw_path = os.getenv("DA_DRUG_RUNTIME_PID_FILE", "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="ascii")
    return path


if __name__ == "__main__":
    main()
