from __future__ import annotations

import logging
import signal
import socket
import threading
import uuid
from dataclasses import dataclass
from time import monotonic

from agent_app.observability.langfuse_exporter import (
    LangfuseExportError,
    LangfuseHttpExporter,
)
from agent_app.observability.outbox import ObservabilityOutbox
from agent_app.persistence.db import SessionLocal, engine
from agent_app.persistence.schema import verify_agent_schema_current
from shared.redaction import safe_exception_summary
from shared.settings import Settings, get_settings

logger = logging.getLogger("agent_app.langfuse_exporter")


@dataclass
class CircuitBreaker:
    failure_threshold: int
    cooldown_seconds: int
    consecutive_failures: int = 0
    open_until: float = 0.0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.open_until = 0.0

    def record_retryable_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.open_until = monotonic() + self.cooldown_seconds

    def remaining_open_seconds(self) -> float:
        return max(0.0, self.open_until - monotonic())


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    settings.require_agent_postgresql()
    settings.require_langfuse_export_config()
    verify_agent_schema_current(engine)

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info(
            "langfuse_exporter_shutdown_requested signal=%s",
            signum,
        )
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_stop)

    run_exporter(stop_event, settings=settings)


def run_exporter(
    stop_event: threading.Event,
    *,
    settings: Settings,
) -> None:
    worker_id = (
        f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"
    )
    outbox = ObservabilityOutbox(SessionLocal, settings=settings)
    exporter = LangfuseHttpExporter(SessionLocal, settings=settings)
    circuit = CircuitBreaker(
        failure_threshold=(
            settings.langfuse_export_circuit_failure_threshold
        ),
        cooldown_seconds=(
            settings.langfuse_export_circuit_cooldown_seconds
        ),
    )
    logger.info(
        "langfuse_exporter_started worker_id=%s sample_rate=%s",
        worker_id,
        settings.langfuse_success_sample_rate,
    )
    try:
        while not stop_event.is_set():
            open_seconds = circuit.remaining_open_seconds()
            if open_seconds > 0:
                logger.warning(
                    "langfuse_exporter_circuit_open retry_in_seconds=%s",
                    round(open_seconds, 1),
                )
                stop_event.wait(min(open_seconds, 60.0))
                continue

            try:
                items = outbox.claim_due(worker_id=worker_id)
            except Exception as exc:
                logger.error(
                    "langfuse_exporter_claim_failed error=%s",
                    safe_exception_summary(exc),
                )
                stop_event.wait(
                    min(settings.langfuse_export_poll_seconds, 60.0)
                )
                continue
            if not items:
                stop_event.wait(settings.langfuse_export_poll_seconds)
                continue

            for item in items:
                if stop_event.is_set():
                    break
                try:
                    exporter.export(item)
                    outbox.complete(item.id)
                    circuit.record_success()
                except LangfuseExportError as exc:
                    retry_scheduled = outbox.fail(
                        item.id,
                        error_code=exc.code,
                        error_message=str(exc),
                        retryable=exc.retryable,
                    )
                    if exc.retryable:
                        circuit.record_retryable_failure()
                    logger.error(
                        "langfuse_export_failed event_type=%s "
                        "attempt=%s retry_scheduled=%s code=%s",
                        item.event_type,
                        item.attempt_count,
                        retry_scheduled,
                        exc.code,
                    )
                except Exception as exc:
                    safe_error = safe_exception_summary(exc)
                    retry_scheduled = outbox.fail(
                        item.id,
                        error_code="UNEXPECTED_EXPORT_ERROR",
                        error_message=safe_error,
                        retryable=True,
                    )
                    circuit.record_retryable_failure()
                    logger.error(
                        "langfuse_export_unexpected_failure "
                        "event_type=%s attempt=%s retry_scheduled=%s "
                        "error=%s",
                        item.event_type,
                        item.attempt_count,
                        retry_scheduled,
                        safe_error,
                    )
    finally:
        exporter.close()
        logger.info("langfuse_exporter_stopped worker_id=%s", worker_id)


if __name__ == "__main__":
    main()
