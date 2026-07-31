from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContractErrorDefinition:
    status_code: int
    message: str
    retryable: bool = False


# Public error codes and messages are copied from the v1.3 interface
# specification. Internal exception names must never be used as public codes.
CONTRACT_ERROR_CATALOG: dict[str, ContractErrorDefinition] = {
    "INVALID_REQUEST": ContractErrorDefinition(
        400,
        "Request schema or required field is invalid.",
    ),
    "UNAUTHORIZED": ContractErrorDefinition(401, "Authorization failed."),
    "IDEMPOTENCY_CONFLICT": ContractErrorDefinition(
        409,
        "The request_id was reused with a different request body.",
    ),
    "REQUEST_IN_PROGRESS": ContractErrorDefinition(
        409,
        "The same request_id is currently being processed.",
        retryable=True,
    ),
    "AI_PROCESSING_ERROR": ContractErrorDefinition(
        500,
        "An internal AI Server processing error occurred.",
        retryable=True,
    ),
    "BACKEND_PROCESSING_ERROR": ContractErrorDefinition(
        500,
        "An internal Backend Server processing error occurred.",
        retryable=True,
    ),
    "BACKEND_MESSAGE_NOT_FOUND": ContractErrorDefinition(
        404,
        "The Backend user message could not be verified.",
    ),
    "CONVERSATION_BUSY": ContractErrorDefinition(
        409,
        "Another request is currently being processed for the conversation.",
        retryable=True,
    ),
    "BACKEND_DB_UNAVAILABLE": ContractErrorDefinition(
        503,
        "The read-only Backend DB connection is unavailable or incompatible.",
        retryable=True,
    ),
    "AI_PROCESSING_TIMEOUT": ContractErrorDefinition(
        504,
        "The AI request exceeded the processing time limit.",
        retryable=True,
    ),
    "FEEDBACK_ACCEPT_FAILED": ContractErrorDefinition(
        500,
        "The feedback request could not be accepted.",
        retryable=True,
    ),
    "FEEDBACK_ENCRYPTION_UNAVAILABLE": ContractErrorDefinition(
        503,
        "Feedback encryption is temporarily unavailable.",
        retryable=True,
    ),
    "NUTRITION_MEAL_NOT_FOUND": ContractErrorDefinition(
        404,
        "The requested nutrition meal was not found.",
    ),
    "NUTRITION_FOOD_NOT_FOUND": ContractErrorDefinition(
        404,
        "The requested nutrition food was not found.",
    ),
    "MEDICATION_DOSE_EVENT_NOT_FOUND": ContractErrorDefinition(
        404,
        "The requested medication dose event was not found.",
    ),
    "RELATED_MEDICATION_DOSE_EVENT_NOT_FOUND": ContractErrorDefinition(
        404,
        "The related medication dose event was not found.",
    ),
    "NOTIFICATION_POLICY_NOT_FOUND": ContractErrorDefinition(
        404,
        "The requested public notification policy was not found.",
    ),
    "VERSION_CONFLICT": ContractErrorDefinition(
        409,
        "The target record changed after it was read.",
    ),
    "CONFIRMATION_MESSAGE_NOT_FOUND": ContractErrorDefinition(
        409,
        "The confirmed user message could not be verified.",
    ),
    "SOURCE_CHAT_REQUEST_NOT_FOUND": ContractErrorDefinition(
        409,
        "The source chat request could not be verified.",
    ),
    "BUSINESS_VALIDATION_FAILED": ContractErrorDefinition(
        422,
        "Request business validation failed.",
    ),
    "POLICY_EFFECTIVE_DATE_RANGE_INVALID": ContractErrorDefinition(
        422,
        "The effective end date precedes the start date.",
    ),
    "POLICY_EFFECTIVE_DATE_RANGE_TOO_LARGE": ContractErrorDefinition(
        422,
        "The effective date range exceeds the allowed maximum.",
    ),
    "POLICY_EFFECTIVE_START_IN_PAST": ContractErrorDefinition(
        422,
        "The requested effective start date is in the past.",
    ),
    "POLICY_BOUNDARY_VIOLATION": ContractErrorDefinition(
        422,
        "The requested policy exceeds an allowed boundary.",
    ),
    "INVALID_POLICY_PROPOSAL": ContractErrorDefinition(
        422,
        "The notification policy proposal is invalid.",
    ),
    "ASYNC_REQUEST_NOT_FOUND": ContractErrorDefinition(
        404,
        "The original asynchronous request was not found.",
    ),
    "CALLBACK_STATE_CONFLICT": ContractErrorDefinition(
        409,
        "The asynchronous request is already in a conflicting terminal state.",
    ),
    "QUEUE_UNAVAILABLE": ContractErrorDefinition(
        503,
        "The asynchronous processing queue is unavailable.",
        retryable=True,
    ),
}


def contract_error_definition(code: str) -> ContractErrorDefinition:
    """Return the authoritative public definition and reject unknown codes."""

    try:
        return CONTRACT_ERROR_CATALOG[code]
    except KeyError as exc:
        raise ValueError(f"unknown_public_contract_error:{code}") from exc
