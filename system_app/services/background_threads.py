from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("uvicorn.error")


def start_daemon_thread(*, name: str, target: Callable[..., Any], args: tuple[Any, ...]) -> threading.Thread:
    def guarded_target() -> None:
        try:
            target(*args)
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.exception("background_thread_failed name=%s error=%s", name, exc)

    thread = threading.Thread(target=guarded_target, name=name, daemon=True)
    thread.start()
    return thread
