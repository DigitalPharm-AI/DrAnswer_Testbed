from __future__ import annotations

from typing import Iterable

from system_app.services.failure_copy import copy_for_phr_error


def public_error_code(error: Exception, *, allowed_codes: Iterable[str], fallback: str) -> str:
    text = str(error or "").strip()
    code = text.split(":", 1)[0].strip()
    return code if code in set(allowed_codes) else fallback


def public_phr_sync_failure_message(error: object) -> str:
    detail = str(getattr(error, "detail", "") or "")
    status_code = getattr(error, "status_code", None)
    return copy_for_phr_error(detail, status_code=status_code if isinstance(status_code, int) else None).body
