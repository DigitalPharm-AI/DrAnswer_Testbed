from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field

from agent_app.integration.approval_state import (
    InternalApprovalBinding,
    InternalApprovalStore,
)
from agent_app.integration.backend_client import (
    BackendV13Client,
    BackendV13ResponseError,
)
from agent_app.integration.mutations import (
    ConfirmedMutationContext,
    notification_policy_request,
    pop_required_expected_version,
    record_change_request_from_tool,
)
from agent_app.integration.write_state import (
    BackendWriteIdentity,
    BackendWriteStateStore,
    canonical_payload_hash,
)
from agent_app.persistence.db import SessionLocal
from agent_app.tools.backend_query import BackendQueryTools
from shared.schemas import ToolCallResult
from shared.settings import get_settings
from shared.tool_names import (
    BACKEND_V13_POLICY_WRITE_TOOLS,
    BACKEND_V13_RECORD_WRITE_TOOLS,
    BACKEND_V13_SYNC_WRITE_TOOLS,
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
)

BackendWriteEndpoint = Literal["record_change", "notification_policy_change"]
INTERNAL_WRITE_REASON = "사용자 채팅 메시지에서 명시적으로 확인된 AI Tool 실행"
MODEL_FORBIDDEN_TECHNICAL_ARGUMENTS = frozenset(
    {
        "patient_id",
        "expected_version",
        "request_id",
        "source_chat_request_id",
        "confirmation_message_id",
        "requested_at",
        "reason",
    }
)
MODEL_WRITE_ARGUMENTS: dict[str, frozenset[str]] = {
    UPDATE_MEDICATION_DOSE_EVENT_STATUS: frozenset({"dose_event_id"}),
    CREATE_MEDICATION_SIDE_EFFECT_RECORD: frozenset(
        {"symptom_text", "symptom_onset_text", "medication_name"}
    ),
    CREATE_NUTRITION_MEAL_RECORD: frozenset(
        {"meal_type", "meal_date", "meal_time", "description", "foods"}
    ),
    UPDATE_NUTRITION_MEAL_RECORD: frozenset(
        {
            "meal_id",
            "meal_type",
            "meal_date",
            "meal_time",
            "scenario_key",
            "description",
            "foods",
        }
    ),
    DELETE_NUTRITION_MEAL_RECORD: frozenset({"meal_id"}),
    UPDATE_NUTRITION_FOOD_RECORD: frozenset(
        {"meal_id", "food_id", "food_ref_id", "food_name", "portion", "nutrients"}
    ),
    DELETE_NUTRITION_FOOD_RECORD: frozenset({"meal_id", "food_id"}),
    CHANGE_NOTIFICATION_POLICY: frozenset({"policy_id", "decision", "changes"}),
}


@dataclass(frozen=True)
class BackendWriteToolSpec:
    tool_name: str
    endpoint: BackendWriteEndpoint
    resource_type: str
    operation: str
    requires_expected_version: bool


@dataclass(frozen=True)
class PreparedBackendWrite:
    request_id: str
    internal_arguments: dict[str, Any]
    cached_response: dict[str, Any] | None


BACKEND_WRITE_TOOL_SPECS: dict[str, BackendWriteToolSpec] = {
    UPDATE_MEDICATION_DOSE_EVENT_STATUS: BackendWriteToolSpec(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        "record_change",
        "medication_dose_event",
        "update",
        True,
    ),
    CREATE_MEDICATION_SIDE_EFFECT_RECORD: BackendWriteToolSpec(
        CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        "record_change",
        "medication_side_effect",
        "create",
        False,
    ),
    CREATE_NUTRITION_MEAL_RECORD: BackendWriteToolSpec(
        CREATE_NUTRITION_MEAL_RECORD,
        "record_change",
        "nutrition_meal",
        "create",
        False,
    ),
    UPDATE_NUTRITION_MEAL_RECORD: BackendWriteToolSpec(
        UPDATE_NUTRITION_MEAL_RECORD,
        "record_change",
        "nutrition_meal",
        "update",
        True,
    ),
    DELETE_NUTRITION_MEAL_RECORD: BackendWriteToolSpec(
        DELETE_NUTRITION_MEAL_RECORD,
        "record_change",
        "nutrition_meal",
        "delete",
        True,
    ),
    UPDATE_NUTRITION_FOOD_RECORD: BackendWriteToolSpec(
        UPDATE_NUTRITION_FOOD_RECORD,
        "record_change",
        "nutrition_food",
        "update",
        True,
    ),
    DELETE_NUTRITION_FOOD_RECORD: BackendWriteToolSpec(
        DELETE_NUTRITION_FOOD_RECORD,
        "record_change",
        "nutrition_food",
        "delete",
        True,
    ),
    CHANGE_NOTIFICATION_POLICY: BackendWriteToolSpec(
        CHANGE_NOTIFICATION_POLICY,
        "notification_policy_change",
        "notification_policy",
        "change",
        True,
    ),
}

if frozenset(BACKEND_WRITE_TOOL_SPECS) != BACKEND_V13_SYNC_WRITE_TOOLS:
    raise RuntimeError("backend_write_tool_registry_mismatch")
if frozenset(MODEL_WRITE_ARGUMENTS) != BACKEND_V13_SYNC_WRITE_TOOLS:
    raise RuntimeError("backend_write_argument_registry_mismatch")
if frozenset(name for name, spec in BACKEND_WRITE_TOOL_SPECS.items() if spec.endpoint == "record_change") != BACKEND_V13_RECORD_WRITE_TOOLS:
    raise RuntimeError("backend_record_write_tool_registry_mismatch")
if frozenset(name for name, spec in BACKEND_WRITE_TOOL_SPECS.items() if spec.endpoint == "notification_policy_change") != BACKEND_V13_POLICY_WRITE_TOOLS:
    raise RuntimeError("backend_policy_write_tool_registry_mismatch")


class BackendWriteInvocationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_chat_request_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    approval_key: str = Field(min_length=1)
    action_fingerprint: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    requested_at: datetime


class BackendSyncWriteTools:
    """Backend 쓰기 API를 같은 Agent turn 안에서 request-response 방식으로 호출한다."""

    def __init__(
        self,
        client: BackendV13Client,
        backend_queries: BackendQueryTools | None = None,
        state_store: BackendWriteStateStore | None = None,
        approval_store: InternalApprovalStore | None = None,
    ) -> None:
        self.client = client
        self.backend_queries = backend_queries
        settings = get_settings()
        self.state_store = state_store or BackendWriteStateStore(
            SessionLocal,
            retention_seconds=settings.agent_backend_write_retention_seconds,
        )
        self.approval_store = approval_store or InternalApprovalStore(
            SessionLocal
        )
        self._request_arguments_lock = anyio.Lock()

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
        context: BackendWriteInvocationContext,
        request_id_override: str | None = None,
        expected_version_override: int | None = None,
        authoritative_arguments_override: dict[str, Any] | None = None,
    ) -> ToolCallResult:
        spec = BACKEND_WRITE_TOOL_SPECS.get(tool_name)
        if spec is None:
            raise ValueError(f"unsupported_backend_write_tool:{tool_name}")
        forbidden = sorted(MODEL_FORBIDDEN_TECHNICAL_ARGUMENTS.intersection(arguments))
        if forbidden:
            raise ValueError(f"technical_arguments_are_tool_managed:{','.join(forbidden)}")
        unsupported = sorted(set(arguments).difference(MODEL_WRITE_ARGUMENTS[tool_name]))
        if unsupported:
            raise ValueError(f"unsupported_model_arguments:{','.join(unsupported)}")

        authoritative_arguments = (
            dict(authoritative_arguments_override)
            if authoritative_arguments_override is not None
            else dict(arguments)
        )
        request_id = (
            request_id_override
            or backend_write_request_id(
                source_chat_request_id=context.source_chat_request_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=authoritative_arguments,
            )
        )
        identity = BackendWriteIdentity(
            request_id=request_id,
            source_chat_request_id=context.source_chat_request_id,
            patient_id_hash=canonical_payload_hash(
                {"patient_id": context.patient_id}
            ),
            trusted_context_hash=canonical_payload_hash(
                {
                    "source_chat_request_id": context.source_chat_request_id,
                    "source_message_id": context.source_message_id,
                    "action_fingerprint": context.action_fingerprint,
                    "patient_id": context.patient_id,
                }
            ),
            tool_call_id=tool_call_id.strip(),
            tool_name=tool_name,
            argument_hash=canonical_payload_hash(authoritative_arguments),
        )
        prepared = await self._stable_internal_arguments(
            identity,
            spec,
            authoritative_arguments,
            patient_id=context.patient_id,
            expected_version_override=expected_version_override,
        )
        request_id = prepared.request_id
        mutation_context = ConfirmedMutationContext(
            request_id=request_id,
            source_chat_request_id=context.source_chat_request_id,
            confirmation_message_id=context.source_message_id,
            patient_id=context.patient_id,
            requested_at=context.requested_at,
        )
        internal_arguments = prepared.internal_arguments
        if spec.endpoint == "record_change":
            request = record_change_request_from_tool(
                tool_name,
                internal_arguments,
                mutation_context,
            )
        else:
            values = dict(internal_arguments)
            request = notification_policy_request(
                context=mutation_context,
                policy_id=str(values.pop("policy_id", "") or ""),
                expected_version=pop_required_expected_version(values),
                decision=str(values.pop("decision", "") or ""),
                changes=values.pop("changes", None),
                reason=str(
                    values.pop("reason", "") or INTERNAL_WRITE_REASON
                ),
            )
            if values:
                raise ValueError(
                    "unsupported_notification_policy_arguments:"
                    f"{','.join(sorted(values))}"
                )
        approval_binding = InternalApprovalBinding(
            approval_key=context.approval_key,
            action_name=tool_name,
            action_fingerprint=context.action_fingerprint,
            patient_id=context.patient_id,
            source_chat_request_id=context.source_chat_request_id,
            confirmation_message_id=context.source_message_id,
            write_request_id=request_id,
            argument_hash=identity.argument_hash,
            request_body_hash=canonical_payload_hash(
                _backend_write_identity_body(request)
            ),
            expected_version=prepared.internal_arguments.get(
                "expected_version"
            ),
        )
        if prepared.cached_response is not None:
            await anyio.to_thread.run_sync(
                lambda: self.approval_store.validate_consumed_replay(
                    approval_binding
                )
            )
            return _tool_result_from_response_body(
                tool_name,
                request_id,
                prepared.cached_response,
            )
        await anyio.to_thread.run_sync(
            lambda: self.approval_store.claim(approval_binding)
        )
        try:
            await anyio.to_thread.run_sync(
                lambda: self.state_store.begin_attempt(request_id)
            )
            if spec.endpoint == "record_change":
                response = await self.client.change_record(request)
            else:
                response = await self.client.change_notification_policy(
                    request
                )
        except BackendV13ResponseError as exc:
            error_response = exc.error_response
            if error_response is None:
                failure_code = type(exc).__name__
                await anyio.to_thread.run_sync(
                    lambda: self.state_store.record_transport_failure(
                        request_id,
                        error_code=failure_code,
                    )
                )
                await anyio.to_thread.run_sync(
                    lambda: self.approval_store.release_retry(
                        context.approval_key
                    )
                )
                raise
            response_body = error_response.model_dump(mode="json")
            error_code = error_response.error.code
            status_code = exc.status_code or 500
            stored = await anyio.to_thread.run_sync(
                lambda: self.state_store.record_response(
                    request_id,
                    status_code=status_code,
                    body=response_body,
                    terminal=not error_response.error.retryable,
                    error_code=error_code,
                )
            )
            if error_response.error.retryable:
                await anyio.to_thread.run_sync(
                    lambda: self.approval_store.release_retry(
                        context.approval_key
                    )
                )
            else:
                await anyio.to_thread.run_sync(
                    lambda: self.approval_store.consume(
                        context.approval_key,
                        result=response_body,
                    )
            )
            return _tool_result_from_response_body(
                tool_name,
                request_id,
                stored.response_body or response_body,
            )
        except Exception as exc:
            failure_code = type(exc).__name__
            await anyio.to_thread.run_sync(
                lambda: self.state_store.record_transport_failure(
                    request_id,
                    error_code=failure_code,
                )
            )
            await anyio.to_thread.run_sync(
                lambda: self.approval_store.release_retry(
                    context.approval_key
                )
            )
            raise

        response_body = response.model_dump(mode="json")
        stored = await anyio.to_thread.run_sync(
            lambda: self.state_store.record_response(
                request_id,
                status_code=200,
                body=response_body,
                terminal=True,
                error_code="",
            )
        )
        authoritative_body = stored.response_body or response_body
        await anyio.to_thread.run_sync(
            lambda: self.approval_store.consume(
                context.approval_key,
                result=authoritative_body,
            )
        )
        return _tool_result_from_response_body(
            tool_name,
            request_id,
            authoritative_body,
        )

    async def _stable_internal_arguments(
        self,
        identity: BackendWriteIdentity,
        spec: BackendWriteToolSpec,
        arguments: dict[str, Any],
        *,
        patient_id: str,
        expected_version_override: int | None,
    ) -> PreparedBackendWrite:
        state = await anyio.to_thread.run_sync(
            lambda: self.state_store.load(identity)
        )
        if state is not None:
            return PreparedBackendWrite(
                request_id=state.request_id,
                internal_arguments=_internal_arguments_from_state(
                    spec,
                    arguments,
                    state.expected_version,
                ),
                cached_response=state.response_body,
            )

        async with self._request_arguments_lock:
            state = await anyio.to_thread.run_sync(
                lambda: self.state_store.load(identity)
            )
            if state is None:
                state = await anyio.to_thread.run_sync(
                    lambda: self.state_store.find_equivalent(identity)
                )
            if state is None:
                resolved = await self._internal_arguments(
                    spec,
                    arguments,
                    patient_id=patient_id,
                    expected_version_override=expected_version_override,
                )
                expected_version = resolved.get("expected_version")
                if not spec.requires_expected_version:
                    expected_version = None
                state = await anyio.to_thread.run_sync(
                    lambda: self.state_store.prepare(
                        identity,
                        expected_version=expected_version,
                    )
                )
            return PreparedBackendWrite(
                request_id=state.request_id,
                internal_arguments=_internal_arguments_from_state(
                    spec,
                    arguments,
                    state.expected_version,
                ),
                cached_response=state.response_body,
            )

    async def _internal_arguments(
        self,
        spec: BackendWriteToolSpec,
        arguments: dict[str, Any],
        *,
        patient_id: str,
        expected_version_override: int | None,
    ) -> dict[str, Any]:
        values = dict(arguments)
        if spec.requires_expected_version:
            if expected_version_override is not None:
                if (
                    isinstance(expected_version_override, bool)
                    or expected_version_override < 0
                ):
                    raise ValueError("expected_version_override_invalid")
                version = expected_version_override
            else:
                if self.backend_queries is None:
                    raise RuntimeError(
                        "backend_read_query_tools_required_for_write"
                    )
                if spec.endpoint == "notification_policy_change":
                    policy_id = str(values.get("policy_id") or "")
                    version = await anyio.to_thread.run_sync(
                        lambda: self.backend_queries.notification_policy_version(
                            patient_id=patient_id,
                            policy_id=policy_id,
                        )
                    )
                else:
                    record_id, parent_record_id = _record_identifiers(
                        spec,
                        values,
                    )
                    version = await anyio.to_thread.run_sync(
                        lambda: self.backend_queries.record_version(
                            patient_id=patient_id,
                            resource_type=spec.resource_type,
                            record_id=record_id,
                            parent_record_id=parent_record_id,
                        )
                    )
            values["expected_version"] = version
        if spec.resource_type != "nutrition_food":
            values["reason"] = INTERNAL_WRITE_REASON
        return values


def backend_write_request_id(
    *,
    source_chat_request_id: str,
    tool_name: str,
    tool_call_id: str,
    arguments: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "source_chat_request_id": source_chat_request_id,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id.strip(),
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"req_{hashlib.sha256(canonical).hexdigest()[:16]}"


def _backend_write_identity_body(request: Any) -> dict[str, Any]:
    """Exclude transport time while preserving every nested business field."""

    body = request.model_dump(mode="json")
    body.pop("requested_at", None)
    return body


def is_backend_v13_sync_write(tool_name: str) -> bool:
    return tool_name in BACKEND_WRITE_TOOL_SPECS


def _record_identifiers(
    spec: BackendWriteToolSpec,
    values: dict[str, Any],
) -> tuple[str | int, str | int | None]:
    if spec.resource_type == "medication_dose_event":
        return values.get("dose_event_id"), None
    if spec.resource_type == "nutrition_meal":
        return values.get("meal_id"), None
    if spec.resource_type == "nutrition_food":
        return values.get("food_id"), values.get("meal_id")
    raise ValueError(f"unsupported_versioned_resource:{spec.resource_type}")


def _internal_arguments_from_state(
    spec: BackendWriteToolSpec,
    arguments: dict[str, Any],
    expected_version: int | None,
) -> dict[str, Any]:
    values = dict(arguments)
    if spec.requires_expected_version:
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise RuntimeError("persisted_expected_version_missing")
        values["expected_version"] = expected_version
    if spec.resource_type != "nutrition_food":
        values["reason"] = INTERNAL_WRITE_REASON
    return values


def _tool_result_from_response_body(
    tool_name: str,
    request_id: str,
    body: dict[str, Any],
) -> ToolCallResult:
    error = body.get("error")
    success = isinstance(body.get("result"), dict) and not isinstance(
        error,
        dict,
    )
    error_code = (
        str(error.get("code") or "")
        if isinstance(error, dict)
        else ""
    )
    retryable = (
        bool(error.get("retryable"))
        if isinstance(error, dict)
        else False
    )
    return ToolCallResult(
        tool_name=tool_name,
        status="success" if success else "error",
        response=body,
        error=error_code,
        idempotency_key=request_id,
        retryable=retryable,
    )
