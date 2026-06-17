from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from shared.settings import get_settings


def require_internal_api_token(x_internal_api_token: str | None = Header(default=None)) -> None:
    settings = get_settings()
    settings.require_internal_api_token_in_production()
    expected_token = settings.internal_api_token
    if not expected_token:
        return
    if not x_internal_api_token or not hmac.compare_digest(x_internal_api_token, expected_token):
        raise HTTPException(status_code=401, detail="invalid_internal_api_token")
