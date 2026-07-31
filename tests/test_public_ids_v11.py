from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from shared.backend_v13_contracts import RecordChangeRequest
from shared.public_ids import (
    AssistantMessageId,
    DoseEventId,
    FoodId,
    MealId,
    NotificationPolicyId,
    PatientId,
    RequestId,
    SideEffectId,
    UserMessageId,
    is_public_id,
    new_public_id,
)
from system_app.models import new_message_public_id


@pytest.mark.parametrize(
    ("kind", "expected_prefix"),
    [
        ("request", "req_"),
        ("patient", "patient_"),
        ("user_message", "user_msg_"),
        ("assistant_message", "assistant_msg_"),
        ("meal", "meal_"),
        ("food", "food_"),
        ("dose_event", "dose_"),
        ("side_effect", "sidefx_"),
        ("notification_policy", "npol_"),
    ],
)
def test_public_id_generators_use_exact_v13_shape(
    kind: str,
    expected_prefix: str,
) -> None:
    value = new_public_id(kind)  # type: ignore[arg-type]

    assert value.startswith(expected_prefix)
    assert len(value.removeprefix(expected_prefix)) == 16
    assert is_public_id(value, kind)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("public_id_type", "valid"),
    [
        (RequestId, "req_0000000000000001"),
        (PatientId, "patient_0000000000000001"),
        (UserMessageId, "user_msg_0000000000000001"),
        (AssistantMessageId, "assistant_msg_0000000000000001"),
        (MealId, "meal_0000000000000001"),
        (FoodId, "food_0000000000000001"),
        (DoseEventId, "dose_0000000000000001"),
        (SideEffectId, "sidefx_0000000000000001"),
        (NotificationPolicyId, "npol_0000000000000001"),
    ],
)
def test_public_id_types_reject_noncanonical_values(
    public_id_type,
    valid: str,
) -> None:
    adapter = TypeAdapter(public_id_type)
    assert adapter.validate_python(valid) == valid

    invalid_values = [
        valid[:-1],
        f"{valid}0",
        valid[:-1] + "A",
        f" {valid}",
        f"{valid.rsplit('_', 1)[0]}_nothex0000000000",
    ]
    for invalid in invalid_values:
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_message_generator_has_no_ambiguous_fallback() -> None:
    assert is_public_id(new_message_public_id("user"), "user_message")
    assert is_public_id(
        new_message_public_id("assistant"),
        "assistant_message",
    )
    with pytest.raises(
        ValueError,
        match="message_public_id_role_must_be_user_or_assistant",
    ):
        new_message_public_id("system")


def test_record_contract_cross_checks_resource_prefix() -> None:
    body = {
        "request_id": "req_0000000000000001",
        "source_chat_request_id": "req_0000000000000002",
        "confirmation_message_id": "user_msg_0000000000000001",
        "patient_id": "patient_0000000000000001",
        "resource_type": "nutrition_food",
        "operation": "update",
        "record_id": "meal_0000000000000001",
        "parent_record_id": "meal_0000000000000002",
        "expected_version": 1,
        "payload": {"food_name": "사과"},
        "requested_at": datetime.now(UTC),
    }

    with pytest.raises(ValidationError, match="invalid_food_id"):
        RecordChangeRequest.model_validate(body)
