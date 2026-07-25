from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field

from agent_app.integration.backend_client import BackendV12Client
from agent_app.integration.mutations import (
    ConfirmedMutationContext,
    notification_policy_request,
    record_change_request_from_tool,
)
from agent_app.tools.backend_query import BackendQueryTools
from agent_app.tools.names import (
    BACKEND_V12_POLICY_WRITE_TOOLS,
    BACKEND_V12_RECORD_WRITE_TOOLS,
    BACKEND_V12_SYNC_WRITE_TOOLS,
    CHANGE_NOTIFICATION_POLICY,
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
)
from shared.schemas import ToolCallResult

BackendWriteEndpoint = Literal["record_change", "notification_policy_change"]
INTERNAL_WRITE_REASON = "사용자 채팅 메시지에서 명시적으로 확인된 AI Tool 실행"
MODEL_FORBIDDEN_TECHNICAL_ARGUMENTS = frozenset(
    {
        "patient_id",
        "expected_version",
        "request_id",
        "source_chat_request_id",
        "conversation_id",
        "confirmation_message_id",
        "requested_at",
        "reason",
    }
)
MODEL_WRITE_ARGUMENTS: dict[str, frozenset[str]] = {
    UPDATE_MEDICATION_DOSE_EVENT_STATUS: frozenset({"dose_event_id", "taken_at"}),
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
    DELETE_NUTRITION_FOOD_RECORD: frozenset(
        {"meal_id", "food_id", "delete_empty_meal"}
    ),
    CHANGE_NOTIFICATION_POLICY: frozenset({"policy_id", "decision", "changes"}),
}


@dataclass(frozen=True)
class BackendWriteToolSpec:
    tool_name: str
    endpoint: BackendWriteEndpoint
    resource_type: str
    operation: str
    requires_expected_version: bool


BACKEND_WRITE_TOOL_SPECS: dict[str, BackendWriteToolSpec] = {
    UPDATE_MEDICATION_DOSE_EVENT_STATUS: BackendWriteToolSpec(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        "record_change",
        "medication_dose_event",
        "update",
        True,
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

if frozenset(BACKEND_WRITE_TOOL_SPECS) != BACKEND_V12_SYNC_WRITE_TOOLS:
    raise RuntimeError("backend_write_tool_registry_mismatch")
if frozenset(MODEL_WRITE_ARGUMENTS) != BACKEND_V12_SYNC_WRITE_TOOLS:
    raise RuntimeError("backend_write_argument_registry_mismatch")
if frozenset(name for name, spec in BACKEND_WRITE_TOOL_SPECS.items() if spec.endpoint == "record_change") != BACKEND_V12_RECORD_WRITE_TOOLS:
    raise RuntimeError("backend_record_write_tool_registry_mismatch")
if frozenset(name for name, spec in BACKEND_WRITE_TOOL_SPECS.items() if spec.endpoint == "notification_policy_change") != BACKEND_V12_POLICY_WRITE_TOOLS:
    raise RuntimeError("backend_policy_write_tool_registry_mismatch")


class BackendWriteInvocationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_chat_request_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    requested_at: datetime


class BackendSyncWriteTools:
    """Backend 쓰기 API를 같은 Agent turn 안에서 request-response 방식으로 호출한다."""

    def __init__(
        self,
        client: BackendV12Client,
        backend_queries: BackendQueryTools | None = None,
    ) -> None:
        self.client = client
        self.backend_queries = backend_queries
        self._request_arguments: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._request_arguments_lock = anyio.Lock()

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
        context: BackendWriteInvocationContext,
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

        request_id = backend_write_request_id(
            source_chat_request_id=context.source_chat_request_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
        )
        mutation_context = ConfirmedMutationContext(
            request_id=request_id,
            source_chat_request_id=context.source_chat_request_id,
            conversation_id=context.conversation_id,
            confirmation_message_id=context.source_message_id,
            patient_id=context.patient_id,
            requested_at=context.requested_at,
        )
        internal_arguments = await self._stable_internal_arguments(
            request_id,
            spec,
            arguments,
            patient_id=context.patient_id,
        )

        if spec.endpoint == "record_change":
            request = record_change_request_from_tool(tool_name, internal_arguments, mutation_context)
            response = await self.client.change_record(request)
        else:
            values = dict(internal_arguments)
            response = await self.client.change_notification_policy(
                notification_policy_request(
                    context=mutation_context,
                    policy_id=str(values.pop("policy_id", "") or ""),
                    expected_version=_required_expected_version(values),
                    decision=str(values.pop("decision", "") or ""),
                    changes=values.pop("changes", None),
                    reason=str(values.pop("reason", "") or INTERNAL_WRITE_REASON),
                )
            )
            if values:
                raise ValueError(f"unsupported_notification_policy_arguments:{','.join(sorted(values))}")

        response_body = response.model_dump(mode="json")
        return ToolCallResult(
            tool_name=tool_name,
            status="success" if response.success else "error",
            response=response_body,
            error=response.error.code if response.error else "",
            idempotency_key=request_id,
        )

    async def _stable_internal_arguments(
        self,
        request_id: str,
        spec: BackendWriteToolSpec,
        arguments: dict[str, Any],
        *,
        patient_id: str,
    ) -> dict[str, Any]:
        async with self._request_arguments_lock:
            cached = self._request_arguments.get(request_id)
            if cached is not None:
                self._request_arguments.move_to_end(request_id)
                return dict(cached)
            resolved = await self._internal_arguments(
                spec,
                arguments,
                patient_id=patient_id,
            )
            self._request_arguments[request_id] = dict(resolved)
            while len(self._request_arguments) > 1024:
                self._request_arguments.popitem(last=False)
            return resolved

    async def _internal_arguments(
        self,
        spec: BackendWriteToolSpec,
        arguments: dict[str, Any],
        *,
        patient_id: str,
    ) -> dict[str, Any]:
        values = dict(arguments)
        if spec.requires_expected_version:
            if self.backend_queries is None:
                raise RuntimeError("backend_read_query_tools_required_for_write")
            if spec.endpoint == "notification_policy_change":
                policy_id = str(values.get("policy_id") or "")
                version = await anyio.to_thread.run_sync(
                    lambda: self.backend_queries.notification_policy_version(
                        patient_id=patient_id,
                        policy_id=policy_id,
                    )
                )
            else:
                record_id, parent_record_id = _record_identifiers(spec, values)
                version = await anyio.to_thread.run_sync(
                    lambda: self.backend_queries.record_version(
                        patient_id=patient_id,
                        resource_type=spec.resource_type,
                        record_id=record_id,
                        parent_record_id=parent_record_id,
                    )
                )
            values["expected_version"] = version
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
    return f"write_{hashlib.sha256(canonical).hexdigest()}"


def is_backend_v12_sync_write(tool_name: str) -> bool:
    return tool_name in BACKEND_WRITE_TOOL_SPECS


def _required_expected_version(values: dict[str, Any]) -> int:
    raw = values.pop("expected_version", None)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError("expected_version_required")
    return raw


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
