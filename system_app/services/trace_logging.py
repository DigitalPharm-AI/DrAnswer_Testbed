from __future__ import annotations

import logging
from typing import Any

from shared.json_utils import dump_json
from shared.redaction import redact_for_logging
from shared.settings import get_settings

logger = logging.getLogger("uvicorn.error")


def enabled() -> bool:
    return get_settings().system_trace_logging


def snippet(value: Any, limit: int = 140) -> str:
    text = "" if value is None else str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def log_info(event: str, **fields: Any) -> None:
    if enabled():
        logger.info("%s %s", event, dump_json(redact_for_logging(fields)))


def log_warning(event: str, **fields: Any) -> None:
    if enabled():
        logger.warning("%s %s", event, dump_json(redact_for_logging(fields)))
