from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from shared.redaction import safe_exception_summary

logger = logging.getLogger("uvicorn.error")


def start_daemon_thread(*, name: str, target: Callable[..., Any], args: tuple[Any, ...]) -> threading.Thread:
    def guarded_target() -> None:
        try:
            target(*args)
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.error("background_thread_failed name=%s error=%s", name, safe_exception_summary(exc))

    thread = threading.Thread(target=guarded_target, name=name, daemon=True)
    thread.start()
    return thread
