from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from agent_app.integration.backend_client import (
    BackendV13Client,
    BackendV13ConfigurationError,
    BackendV13ResponseError,
    backend_request_attempt_count,
)
from agent_app.integration.mutations import (
    ConfirmedMutationContext,
    notification_policy_request,
    record_change_request_from_tool,
)
from agent_app.tools.backend_write import (
    BACKEND_WRITE_TOOL_SPECS,
    BackendSyncWriteTools,
    _internal_arguments_from_state,
)
from shared.backend_v13_contracts import (
    MedicationSideEffectMutationPayload,
    NotificationPolicyChangeRequest,
    NotificationPolicyChanges,
    NutritionFoodMutationPayload,
    RecordChangeRequest,
)
from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
)

NOW = datetime(2026, 7, 25, 12, 35, tzinfo=UTC)


def mutation_context(*, request_id: str = "req_0000000000000001") -> ConfirmedMutationContext:
    return ConfirmedMutationContext(
        request_id=request_id,
        source_chat_request_id="req_0000000000000015",
        confirmation_message_id="user_msg_0000000000000115",
        patient_id="patient_0000000000000123",
        requested_at=NOW,
    )


def meal_create_request() -> RecordChangeRequest:
    return record_change_request_from_tool(
        CREATE_NUTRITION_MEAL_RECORD,
        {
            "patient_id": "patient_0000000000000123",
            "meal_type": "lunch",
            "meal_date": "2026-07-25",
            "meal_time": "12:30",
            "foods": [
                {
                    "food_ref_id": "FOOD-001",
                    "food_name": "구운 닭가슴살",
                    "portion": "100g",
                    "nutrients": {"calories": 165, "protein": 31},
                }
            ],
        },
        mutation_context(),
    )


def successful_record_response(request_id: str = "req_0000000000000001") -> dict:
    return {
        "request_id": request_id,
        "result": {
            "resource_type": "nutrition_meal",
            "operation": "create",
            "record_id": "meal_0000000000000042",
            "parent_record_id": None,
            "version": 1,
        },
        "processed_at": "2026-07-25T12:35:01+00:00",
    }


def failed_record_response(request_id: str, code: str, *, retryable: bool) -> dict:
    return {
        "request_id": request_id,
        "error": {
            "code": code,
            "message": code,
            "retryable": retryable,
            "details": None,
        },
    }


def test_record_contract_maps_meal_create_without_trace_id() -> None:
    request = meal_create_request()

    assert request.resource_type == "nutrition_meal"
    assert request.operation == "create"
    assert request.record_id is None
    assert request.expected_version is None
    assert request.payload is not None
    dumped = request.model_dump(mode="json")
    assert "trace_id" not in dumped
    assert {
        "record_id",
        "parent_record_id",
        "expected_version",
        "payload",
    } <= set(dumped)


def test_record_contract_rejects_external_approval_key() -> None:
    body = meal_create_request().model_dump(mode="json")
    body["approval_key"] = "must-never-cross-the-http-boundary"

    with pytest.raises(ValidationError, match="approval_key"):
        RecordChangeRequest.model_validate(body)


@pytest.mark.parametrize(
    "field",
    ["record_id", "parent_record_id", "expected_version", "payload"],
)
def test_record_contract_requires_nullable_keys(field: str) -> None:
    body = meal_create_request().model_dump(mode="json")
    body.pop(field)

    with pytest.raises(ValidationError):
        RecordChangeRequest.model_validate(body)


def test_record_contract_rejects_external_trace_id() -> None:
    body = meal_create_request().model_dump(mode="json")
    body["trace_id"] = "internal-trace"

    with pytest.raises(ValidationError):
        RecordChangeRequest.model_validate(body)


def test_side_effect_payload_rejects_external_source_trace_id() -> None:
    with pytest.raises(ValidationError):
        MedicationSideEffectMutationPayload.model_validate(
            {
                "symptom_text": "메스꺼움",
                "suspected": True,
                "source_trace_id": "must-stay-inside-ai-server",
            }
        )


def test_record_adapter_maps_food_update_and_parent_record() -> None:
    request = record_change_request_from_tool(
        UPDATE_NUTRITION_FOOD_RECORD,
        {
            "meal_id": "meal_0000000000000042",
            "food_id": "food_0000000000000051",
            "expected_version": 3,
            "portion": "50g",
        },
        mutation_context(request_id="req_0000000000000002"),
    )

    assert request.resource_type == "nutrition_food"
    assert request.operation == "update"
    assert request.record_id == "food_0000000000000051"
    assert request.parent_record_id == "meal_0000000000000042"
    assert request.expected_version == 3
    assert request.payload is not None
    assert request.payload.portion == "50g"
    assert "reason" not in request.payload.model_dump(mode="json")


def test_nutrition_food_payload_rejects_removed_reason_field() -> None:
    with pytest.raises(ValidationError):
        NutritionFoodMutationPayload.model_validate(
            {
                "portion": "50g",
                "reason": "removed-by-v1.3-contract",
            }
        )


@pytest.mark.asyncio
async def test_nutrition_food_internal_write_does_not_inject_reason() -> None:
    tools = BackendSyncWriteTools(client=object())
    spec = BACKEND_WRITE_TOOL_SPECS[UPDATE_NUTRITION_FOOD_RECORD]
    arguments = {
        "meal_id": "meal_0000000000000042",
        "food_id": "food_0000000000000051",
        "portion": "50g",
    }

    prepared = await tools._internal_arguments(
        spec,
        arguments,
        patient_id="patient_0000000000000123",
        expected_version_override=3,
    )
    replayed = _internal_arguments_from_state(
        spec,
        arguments,
        3,
    )

    assert prepared == {**arguments, "expected_version": 3}
    assert replayed == prepared


def test_record_adapter_requires_version_for_update() -> None:
    with pytest.raises(ValueError, match="expected_version_required"):
        record_change_request_from_tool(
            UPDATE_NUTRITION_FOOD_RECORD,
            {
                "meal_id": "meal_0000000000000042",
                "food_id": "food_0000000000000051",
                "portion": "50g",
            },
            mutation_context(),
        )


def test_record_adapter_rejects_model_managed_delete_behavior() -> None:
    with pytest.raises(ValueError, match="delete_empty_meal_is_tool_managed"):
        record_change_request_from_tool(
            DELETE_NUTRITION_FOOD_RECORD,
            {
                "meal_id": "meal_0000000000000042",
                "food_id": "food_0000000000000051",
                "expected_version": 3,
                "delete_empty_meal": False,
            },
            mutation_context(),
        )


def test_record_adapter_maps_medication_update_and_drops_internal_trace() -> None:
    request = record_change_request_from_tool(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        {
            "dose_event_id": "dose_0000000000000120",
            "expected_version": 7,
            "reason": "사용자 확인",
            "source_trace_id": "internal-only",
            "source_event_type": "medication_agent",
        },
        mutation_context(request_id="req_0000000000000003"),
    )

    dumped = request.model_dump(mode="json")
    assert dumped["resource_type"] == "medication_dose_event"
    assert dumped["payload"]["status"] == "taken"
    assert request.payload is not None
    assert request.payload.taken_at == NOW
    assert "source_trace_id" not in dumped["payload"]
    assert "trace_id" not in dumped


def test_policy_contract_supports_apply_and_keep() -> None:
    apply_request = notification_policy_request(
        context=mutation_context(request_id="req_0000000010000001"),
        policy_id="npol_0000000000000001",
        expected_version=2,
        decision="apply",
        changes={"extra_reminders": 2, "interval_minutes": 10},
        reason="사용자 승인",
    )
    keep_request = notification_policy_request(
        context=mutation_context(request_id="req_0000000010000002"),
        policy_id="npol_0000000000000001",
        expected_version=2,
        decision="keep",
        changes=None,
        reason="기존 정책 유지",
    )

    assert apply_request.payload.decision == "apply"
    assert apply_request.payload.changes is not None
    assert keep_request.payload.decision == "keep"
    assert keep_request.payload.changes is None
    assert "trace_id" not in keep_request.model_dump(mode="json")


def test_policy_contract_rejects_keep_with_changes() -> None:
    body = notification_policy_request(
        context=mutation_context(request_id="req_0000000010000002"),
        policy_id="npol_0000000000000001",
        expected_version=2,
        decision="keep",
        changes=None,
        reason="기존 정책 유지",
    ).model_dump(mode="json")
    body["payload"]["changes"] = {"extra_reminders": 2}

    with pytest.raises(ValidationError):
        NotificationPolicyChangeRequest.model_validate(body)


def test_policy_changes_reject_model_managed_fields_and_out_of_range_values() -> None:
    with pytest.raises(ValidationError):
        NotificationPolicyChanges.model_validate(
            {"medication_title_template": "모델이 만든 문구"}
        )
    with pytest.raises(ValidationError):
        NotificationPolicyChanges.model_validate({"interval_minutes": 61})


@pytest.mark.asyncio
async def test_backend_client_retries_503_with_identical_request_and_bearer_token() -> None:
    captured: list[dict] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append(
            {
                "body": body,
                "authorization": request.headers.get("Authorization"),
                "url": str(request.url),
            }
        )
        if len(captured) < 3:
            return httpx.Response(
                503,
                json=failed_record_response(body["request_id"], "BACKEND_TEMPORARILY_UNAVAILABLE", retryable=True),
            )
        return httpx.Response(200, json=successful_record_response(body["request_id"]))

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = BackendV13Client(
        base_url="https://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path="/agent/sync/notification-policy-change",
        bearer_token="secret-token",
        max_retries=2,
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )

    response = await client.change_record(meal_create_request())

    assert response.result.record_id == "meal_0000000000000042"
    assert len(captured) == 3
    assert captured[0]["body"] == captured[1]["body"] == captured[2]["body"]
    assert captured[0]["authorization"] == "Bearer secret-token"
    assert captured[0]["url"] == "https://backend.test/agent/sync/record-change"
    assert sleeps == [1.0, 2.0]
    assert backend_request_attempt_count() == 3


@pytest.mark.asyncio
async def test_backend_client_retries_retryable_request_in_progress() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content.decode("utf-8"))
        if calls >= 3:
            return httpx.Response(
                200,
                json=successful_record_response(body["request_id"]),
            )
        return httpx.Response(
            409,
            json=failed_record_response(body["request_id"], "REQUEST_IN_PROGRESS", retryable=True),
            headers={"Retry-After": "0.25"},
        )

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = BackendV13Client(
        base_url="https://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path="/agent/sync/notification-policy-change",
        bearer_token="Bearer secret-token",
        max_retries=2,
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )

    response = await client.change_record(meal_create_request())

    assert response.result.record_id == "meal_0000000000000042"
    assert calls == 3
    assert sleeps == [0.25, 0.25]
    assert backend_request_attempt_count() == 3


@pytest.mark.asyncio
async def test_backend_client_does_not_retry_non_retryable_409() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            409,
            json=failed_record_response(
                body["request_id"],
                "VERSION_CONFLICT",
                retryable=False,
            ),
        )

    client = BackendV13Client(
        base_url="https://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path="/agent/sync/notification-policy-change",
        bearer_token="secret-token",
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BackendV13ResponseError) as exc_info:
        await client.change_record(meal_create_request())
    assert exc_info.value.status_code == 409
    assert exc_info.value.error_response is not None
    assert (
        exc_info.value.error_response.error.code
        == "VERSION_CONFLICT"
    )
    assert calls == 1
    assert backend_request_attempt_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "expected_message"),
    [
        (
            200,
            failed_record_response(
                "req_0000000000000001",
                "VERSION_CONFLICT",
                retryable=False,
            ),
            "backend_success_contract_invalid:200",
        ),
        (
            409,
            successful_record_response("req_0000000000000001"),
            "backend_error_contract_invalid:409",
        ),
    ],
)
async def test_backend_client_rejects_status_body_contract_mismatch(
    status_code: int,
    body: dict,
    expected_message: str,
) -> None:
    client = BackendV13Client(
        base_url="https://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path=(
            "/agent/sync/notification-policy-change"
        ),
        bearer_token="secret-token",
        max_retries=0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status_code,
                json=body,
            )
        ),
    )

    with pytest.raises(
        BackendV13ResponseError,
        match=expected_message,
    ):
        await client.change_record(meal_create_request())


@pytest.mark.asyncio
async def test_backend_client_rejects_mismatched_response_identifiers() -> None:
    request = meal_create_request()

    def wrong_request_id(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=successful_record_response("req_00000000deadbeef"),
        )

    client = BackendV13Client(
        base_url="https://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path="/agent/sync/notification-policy-change",
        bearer_token="secret-token",
        transport=httpx.MockTransport(wrong_request_id),
    )
    with pytest.raises(
        BackendV13ResponseError,
        match="backend_response_request_id_mismatch",
    ):
        await client.change_record(request)

    def wrong_target(_: httpx.Request) -> httpx.Response:
        response = successful_record_response(request.request_id)
        response["result"]["resource_type"] = "medication_dose_event"
        response["result"]["operation"] = "update"
        response["result"]["record_id"] = "dose_0000000000000042"
        return httpx.Response(200, json=response)

    client = BackendV13Client(
        base_url="https://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path="/agent/sync/notification-policy-change",
        bearer_token="secret-token",
        transport=httpx.MockTransport(wrong_target),
    )
    with pytest.raises(
        BackendV13ResponseError,
        match="backend_response_record_operation_mismatch",
    ):
        await client.change_record(request)


@pytest.mark.asyncio
async def test_backend_client_requires_configured_authorization() -> None:
    client = BackendV13Client(
        base_url="https://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path="/agent/sync/notification-policy-change",
        bearer_token="",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )

    with pytest.raises(BackendV13ConfigurationError, match="service_api_token_required"):
        await client.change_record(meal_create_request())
