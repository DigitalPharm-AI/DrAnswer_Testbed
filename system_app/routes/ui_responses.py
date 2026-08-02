from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from system_app.ui_contracts import UiErrorResponse

_UI_ERROR_DESCRIPTIONS = {
    403: "The UI action is not available in this environment.",
    404: "The requested UI resource was not found.",
    409: "The UI request conflicts with the current or idempotent state.",
    422: "The UI request schema, path, or query value is invalid.",
    500: "The Backend could not complete the UI request.",
    502: "The AI Server returned an invalid response or was unavailable.",
}


class UiResponseContractError(RuntimeError):
    pass


def ui_error_responses(*status_codes: int) -> dict[int, dict[str, Any]]:
    return {
        status_code: {
            "model": UiErrorResponse,
            "description": _UI_ERROR_DESCRIPTIONS[status_code],
        }
        for status_code in status_codes
    }


def ui_success(
    response_model: type[BaseModel],
    data: Any,
) -> JSONResponse:
    return ui_contract_response(
        response_model,
        status_code=200,
        body={
            "success": True,
            "data": data,
            "error": None,
        },
    )


def ui_success_body(
    response_model: type[BaseModel],
    data: Any,
) -> dict[str, Any]:
    return _validated_ui_response_body(
        response_model,
        {
            "success": True,
            "data": data,
            "error": None,
        },
    )


def ui_contract_response(
    response_model: type[BaseModel],
    *,
    status_code: int,
    body: Any,
) -> JSONResponse:
    contract_model = response_model if status_code == 200 else UiErrorResponse
    content = _validated_ui_response_body(contract_model, body)
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(content),
    )


def _validated_ui_response_body(
    response_model: type[BaseModel],
    body: Any,
) -> dict[str, Any]:
    try:
        response = response_model.model_validate(body)
    except ValidationError as exc:
        raise UiResponseContractError("ui_response_contract_validation_failed") from exc
    return response.model_dump(mode="json")


def ui_error_body(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details,
        },
    }


def ui_http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "details": details,
        },
    )
