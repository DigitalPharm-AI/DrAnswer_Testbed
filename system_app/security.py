from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.settings import get_settings

backend_api_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="BackendApiBearer",
    description="AI Server가 Backend v1.3 쓰기 API를 호출할 때 사용하는 Bearer token",
)


def require_internal_api_token(x_internal_api_token: str | None = Header(default=None)) -> None:
    settings = get_settings()
    expected_token = settings.require_internal_api_token()
    if not x_internal_api_token or not hmac.compare_digest(x_internal_api_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_internal_api_token",
        )


def require_backend_api_bearer_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(backend_api_bearer),
    ] = None,
) -> None:
    settings = get_settings()
    expected_token = settings.require_service_api_token()
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, expected_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHORIZED",
                "message": "Authorization failed.",
                "retryable": False,
                "details": None,
            },
        )
