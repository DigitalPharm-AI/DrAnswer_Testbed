from __future__ import annotations

from datetime import date, datetime
from time import perf_counter
from typing import Any

import httpx
import anyio

from agent_app import trace_logging
from agent_app.ae_pro_ctcae import (
    ProCtcaeReferenceUnavailable,
    match_pro_ctcae_symptom,
)
from agent_app.integration.backend_client import (
    BackendV13Client,
    BackendV13ResponseError,
    BackendV13TransportError,
)
from shared.backend_v13_contracts import ProCtcaeSeverityResult
from shared.tool_confirmations import ConfirmationActionRegistry
from agent_app.llm.context import context_value
from shared.tool_catalog import ToolCatalog
from agent_app.tools.backend_query import BackendQueryTools
from agent_app.tools.medication_side_effects import (
    assess_side_effect_from_snapshot,
    side_effect_record_draft_from_snapshot,
)
from agent_app.tools.backend_write import (
    BackendSyncWriteTools,
    BackendWriteInvocationContext,
    MODEL_WRITE_ARGUMENTS,
    is_backend_v13_sync_write,
)
from shared.tool_names import (
    CHANGE_NOTIFICATION_POLICY,
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NOTIFICATION_POLICIES,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    RECORD_APPROVAL_ACTIONS,
    REQUEST_RECORD_APPROVAL,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from shared.tool_permissions import allowed_tool_names_for_source, permission_denied_result, validate_tool_permission
from agent_app.tools.policy import DEFERRED_POLICY_TOOL_NAMES, deferred_policy_tool_result
from agent_app.tools.protocol import (
    ALLOWED_TOOL_NAMES,
    MCP_METHOD_TOOLS_CALL,
    MCP_METHOD_TOOLS_LIST,
    mcp_error_response,
    mcp_result_from_tool_result,
    mcp_success_response,
    mcp_tools_list,
    safe_tool_error,
)
from shared.redaction import safe_exception_summary
from shared.schemas import (
    AEProCtcaeAssessmentRequest,
    MedicationDoseStatusResult,
    SideEffectHistoryResult,
    ToolCallResult,
)
from shared.settings import get_settings

JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601


def _project_record_arguments_to_action_schema(
    action_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Keep only fields declared by the selected canonical write action."""

    action_tools = ToolCatalog.tools_for(action_name)
    if len(action_tools) != 1:
        raise ValueError("record_approval_action_schema_missing")
    input_schema = action_tools[0].get("inputSchema")
    if not isinstance(input_schema, dict):
        raise ValueError("record_approval_action_schema_missing")
    projected = _project_value_to_schema(arguments, input_schema)
    if not isinstance(projected, dict):
        raise ValueError("record_approval_arguments_invalid")
    return projected


def _project_value_to_schema(value: Any, schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        return {
            key: _project_value_to_schema(child, properties[key])
            for key, child in value.items()
            if key in properties and isinstance(properties[key], dict)
        }
    if schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return list(value)
        return [
            _project_value_to_schema(item, item_schema)
            for item in value
        ]
    return value


def _approval_display(
    action_name: str,
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the non-secret approval card owned by the AI Server."""

    if action_name == CREATE_MEDICATION_SIDE_EFFECT_RECORD:
        display = {
            "title": "부작용 평가 기록",
            "question": "다음 부작용 평가 결과를 기록할까요?",
            "tables": [
                {
                    "table_title": None,
                    "rows": _side_effect_approval_rows(
                        arguments,
                        display_context=display_context,
                    ),
                }
            ],
            "action_label": "기록",
        }
    elif action_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        display = {
            "title": "복약 완료 기록",
            "question": "선택한 복약 일정을 복약 완료로 기록할까요?",
            "action_label": "기록",
        }
    elif action_name == CREATE_NUTRITION_MEAL_RECORD:
        display = {
            "title": "식사 기록",
            "question": "다음 식사 내용을 기록할까요?",
            "tables": [
                {
                    "table_title": None,
                    "rows": _nutrition_meal_approval_rows(arguments),
                }
            ],
            "action_label": "기록",
        }
    elif action_name == UPDATE_NUTRITION_MEAL_RECORD:
        display = {
            "title": "식사 기록 수정",
            "question": "확인한 내용으로 식사 기록을 수정할까요?",
            "action_label": "수정",
        }
    elif action_name == DELETE_NUTRITION_MEAL_RECORD:
        display = {
            "title": "식사 기록 삭제",
            "question": "선택한 식사 기록을 삭제할까요?",
            "action_label": "삭제",
        }
    elif action_name == UPDATE_NUTRITION_FOOD_RECORD:
        display = {
            "title": "음식 기록 수정",
            "question": "확인한 내용으로 음식 기록을 수정할까요?",
            "action_label": "수정",
        }
    elif action_name == DELETE_NUTRITION_FOOD_RECORD:
        display = {
            "title": "음식 기록 삭제",
            "question": "선택한 음식 기록을 삭제할까요?",
            "action_label": "삭제",
        }
    elif action_name == CHANGE_NOTIFICATION_POLICY:
        keep = str(arguments.get("decision") or "") == "keep"
        display = {
            "title": "알림 정책 확인",
            "question": (
                "현재 알림 정책을 유지할까요?"
                if keep
                else "확인한 내용으로 알림 정책을 변경할까요?"
            ),
            "action_label": "유지" if keep else "변경",
        }
    else:
        display = {
            "title": "기록 확인",
            "question": "확인한 내용을 적용할까요?",
            "action_label": "적용",
        }
    return _with_approval_time_rows(
        display,
        action_name=action_name,
        arguments=arguments,
        display_context=display_context,
    )


def _with_approval_time_rows(
    display: dict[str, Any],
    *,
    action_name: str,
    arguments: dict[str, Any],
    display_context: dict[str, Any] | None,
) -> dict[str, Any]:
    context = display_context or {}
    requested_at = _display_datetime(context.get("requested_at"))
    target_time = _record_target_time(
        action_name,
        arguments,
        display_context=context,
    )
    time_rows = [
        {
            "column": "기록 요청 시간",
            "value": requested_at or "확인되지 않음",
        },
        {
            "column": "기록 대상 시간",
            "value": target_time or "확인되지 않음",
        },
    ]

    tables = display.get("tables")
    normalized_tables = (
        [
            {
                **table,
                "rows": list(table.get("rows") or []),
            }
            for table in tables
            if isinstance(table, dict)
        ]
        if isinstance(tables, list)
        else []
    )
    if normalized_tables:
        normalized_tables[0]["rows"].extend(time_rows)
    else:
        normalized_tables = [
            {
                "table_title": None,
                "rows": time_rows,
            }
        ]
    return {
        **display,
        "tables": normalized_tables,
    }


def _record_target_time(
    action_name: str,
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any],
) -> str:
    if action_name == CREATE_MEDICATION_SIDE_EFFECT_RECORD:
        return str(
            arguments.get("symptom_onset_text")
            or display_context.get("symptom_onset_text")
            or ""
        ).strip()

    if action_name == CREATE_NUTRITION_MEAL_RECORD:
        return _meal_target_time(
            arguments,
            requested_at=display_context.get("requested_at"),
            default_to_requested_date=True,
        )

    if action_name == UPDATE_NUTRITION_MEAL_RECORD:
        snapshot_target = _meal_target_time_from_snapshot(
            arguments,
            display_context=display_context,
            apply_updates=True,
        )
        if snapshot_target:
            return snapshot_target
        return _meal_target_time(
            arguments,
            requested_at=display_context.get("requested_at"),
            default_to_requested_date=False,
        )

    if action_name in {
        DELETE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_FOOD_RECORD,
        DELETE_NUTRITION_FOOD_RECORD,
    }:
        return _meal_target_time_from_snapshot(
            arguments,
            display_context=display_context,
            apply_updates=False,
        )

    if action_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        return _dose_target_time_from_snapshot(
            arguments,
            display_context=display_context,
        )

    if action_name == CHANGE_NOTIFICATION_POLICY:
        changes = arguments.get("changes")
        if isinstance(changes, dict):
            start = str(changes.get("effective_start_date") or "").strip()
            end = str(changes.get("effective_end_date") or "").strip()
            if start and end:
                return f"{start} ~ {end}"
            if start:
                return f"{start}부터"
            if end:
                return f"{end}까지"
        return "사용자 승인 시점부터"

    if action_name == UPSERT_NUTRITION_PREFERENCE_FACT:
        return "사용자 승인 시점부터"
    return ""


def _meal_target_time(
    arguments: dict[str, Any],
    *,
    requested_at: Any,
    default_to_requested_date: bool,
) -> str:
    meal_date = str(arguments.get("meal_date") or "").strip()
    meal_time = str(arguments.get("meal_time") or "").strip()
    meal_type = str(arguments.get("meal_type") or "").strip()
    if not meal_date and default_to_requested_date:
        meal_date = _display_date(requested_at)
    if not meal_date:
        return ""
    if meal_time:
        return f"{meal_date} {meal_time[:5]}"
    meal_type_label = _meal_type_label(meal_type)
    return (
        f"{meal_date} {meal_type_label}"
        if meal_type_label
        else meal_date
    )


def _meal_target_time_from_snapshot(
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any],
    apply_updates: bool,
) -> str:
    meal_id = str(arguments.get("meal_id") or "").strip()
    if not meal_id:
        return ""
    snapshot = display_context.get("trusted_patient_context")
    if not isinstance(snapshot, dict):
        return ""
    meals = snapshot.get("today_meals")
    if not isinstance(meals, list):
        return ""
    for meal in meals:
        if not isinstance(meal, dict):
            continue
        candidate_id = str(
            meal.get("meal_id") or meal.get("id") or ""
        ).strip()
        if candidate_id != meal_id:
            continue
        target_values = dict(meal)
        if apply_updates:
            for key in ("meal_date", "meal_time", "meal_type"):
                if arguments.get(key) is not None:
                    target_values[key] = arguments[key]
        return _meal_target_time(
            target_values,
            requested_at=display_context.get("requested_at"),
            default_to_requested_date=False,
        )
    return ""


def _dose_target_time_from_snapshot(
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any],
) -> str:
    dose_event_id = str(
        arguments.get("dose_event_id") or ""
    ).strip()
    if not dose_event_id:
        return ""
    snapshot = display_context.get("trusted_patient_context")
    if not isinstance(snapshot, dict):
        return ""
    today_medication = snapshot.get("today_medication")
    if not isinstance(today_medication, dict):
        return ""
    events = today_medication.get("dose_events")
    if not isinstance(events, list):
        return ""
    for event in events:
        if not isinstance(event, dict):
            continue
        candidate_id = str(
            event.get("dose_event_id") or event.get("id") or ""
        ).strip()
        if candidate_id == dose_event_id:
            return _display_datetime(event.get("scheduled_for"))
    return ""


def _display_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return text
    return parsed.strftime("%Y-%m-%d %H:%M")


def _display_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return ""


def _meal_type_label(meal_type: str) -> str:
    return {
        "breakfast": "아침",
        "lunch": "점심",
        "dinner": "저녁",
        "snack": "간식",
    }.get(meal_type, meal_type)


def _nutrition_meal_approval_rows(
    arguments: dict[str, Any],
) -> list[dict[str, str]]:
    meal_type = str(arguments.get("meal_type") or "").strip()
    meal_type_label = _meal_type_label(meal_type) or "확인 필요"

    food_names: list[str] = []
    foods = arguments.get("foods")
    if isinstance(foods, list):
        for food in foods:
            if not isinstance(food, dict):
                continue
            food_name = str(food.get("food_name") or "").strip()
            if food_name and food_name not in food_names:
                food_names.append(food_name)

    return [
        {"column": "시기", "value": meal_type_label},
        {
            "column": "음식 종류",
            "value": ", ".join(food_names) or "확인 필요",
        },
    ]


def _side_effect_approval_rows(
    arguments: dict[str, Any],
    *,
    display_context: dict[str, Any] | None,
) -> list[dict[str, str]]:
    context = display_context or {}
    symptom_name = str(context.get("symptom_name") or "").strip()
    if not symptom_name:
        symptom_name = _symptom_name_from_matched_effects(arguments)
    if not symptom_name:
        symptom_name = str(
            arguments.get("symptom_text") or "증상"
        ).strip()

    rows = [{"column": "증상", "value": symptom_name}]
    rows.extend(_pro_ctcae_response_rows(arguments.get("severity")))

    matched_items = _unique_text_values(
        arguments.get("matched_items")
    )
    if matched_items:
        rows.append(
            {
                "column": "관련 가능 약물",
                "value": ", ".join(matched_items),
            }
        )
    return rows


def _symptom_name_from_matched_effects(
    arguments: dict[str, Any],
) -> str:
    for value in _unique_text_values(
        arguments.get("matched_effects")
    ):
        _, separator, effect = value.partition(":")
        normalized = effect.strip() if separator else value.strip()
        if normalized:
            return normalized
    return ""


def _pro_ctcae_response_rows(
    severity: Any,
) -> list[dict[str, str]]:
    if not isinstance(severity, dict):
        return []
    questions = severity.get("questions")
    responses = severity.get("responses")
    if not isinstance(questions, list) or not isinstance(
        responses,
        list,
    ):
        return []

    questions_by_code = {
        str(question.get("item_code") or ""): question
        for question in questions
        if isinstance(question, dict)
    }
    rows: list[dict[str, str]] = []
    for index, response in enumerate(responses):
        if not isinstance(response, dict):
            continue
        response_text = str(
            response.get("response_text") or ""
        ).strip()
        if not response_text:
            continue
        item_code = str(response.get("item_code") or "")
        question = questions_by_code.get(item_code)
        question_text = (
            str(question.get("question") or "").strip()
            if isinstance(question, dict)
            else ""
        )
        response_type = (
            str(question.get("response_type") or "").strip()
            if isinstance(question, dict)
            else ""
        )
        rows.append(
            {
                "column": (
                    response_type
                    or question_text
                    or f"문항 {index + 1}"
                ),
                "value": response_text,
            }
        )
    return rows


def _unique_text_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


class AgentMcpToolServer:
    def __init__(
        self,
        *,
        system_base_url: str | None = None,
        timeout_seconds: float = 30.0,
        backend_queries: BackendQueryTools | None = None,
        backend_client: BackendV13Client | None = None,
    ) -> None:
        settings = get_settings()
        # Kept as a constructor compatibility argument only. Approval state is
        # Agent-owned and no longer calls a Backend "prepare" extension.
        del system_base_url
        self.timeout_seconds = timeout_seconds
        self.backend_queries = backend_queries or BackendQueryTools.from_settings(settings)
        self.backend_client = backend_client or BackendV13Client.from_settings()
        self.backend_writes = BackendSyncWriteTools(
            self.backend_client,
            self.backend_queries,
        )

    async def handle_json_rpc(self, request: dict[str, Any], *, trace_id: str, source_event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return mcp_error_response(request_id, JSON_RPC_INVALID_REQUEST, "invalid_json_rpc_request")
        method = str(request["method"])
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if method == MCP_METHOD_TOOLS_LIST:
            return mcp_success_response(request_id, self.tools_list(source_event_type=source_event_type))
        if method == MCP_METHOD_TOOLS_CALL:
            return mcp_success_response(
                request_id,
                await self.tools_call(params, trace_id=trace_id, source_event_type=source_event_type, payload=payload),
            )
        return mcp_error_response(request_id, JSON_RPC_METHOD_NOT_FOUND, f"method_not_found:{method}")

    def tools_list(self, *, source_event_type: str = "mcp") -> dict[str, Any]:
        allowed = allowed_tool_names_for_source(source_event_type)
        tools = ToolCatalog.tools_for(*sorted(allowed))
        return mcp_tools_list(tools, source_event_type=source_event_type, allowed_tool_names=sorted(allowed))

    async def tools_call(self, params: dict[str, Any], *, trace_id: str, source_event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        tool_call_id = str(params.get("tool_call_id") or "")
        started = perf_counter()
        trace_logging.log_info(
            "agent_mcp_tools_call_started",
            trace_id=trace_id,
            source_event_type=source_event_type,
            tool=tool_name,
            argument_keys=sorted(str(key) for key in arguments.keys()),
        )
        try:
            result = await self._execute_tool_result(
                tool_name,
                arguments,
                trace_id=trace_id,
                source_event_type=source_event_type,
                payload=payload,
                tool_call_id=tool_call_id,
            )
        except BackendV13ResponseError as exc:
            error_response = exc.error_response
            result = ToolCallResult(
                tool_name=tool_name or "unknown",
                status="error",
                error=(
                    error_response.error.code
                    if error_response is not None
                    else "backend_response_contract_invalid"
                ),
                idempotency_key=(
                    error_response.request_id
                    if error_response is not None
                    and error_response.request_id is not None
                    else f"{trace_id}:{tool_name}"
                ),
                response=(
                    error_response.model_dump(mode="json")
                    if error_response is not None
                    else {}
                ),
                elapsed_ms=round(
                    (perf_counter() - started) * 1000
                ),
                retryable=bool(
                    error_response is not None
                    and error_response.error.retryable
                ),
            )
        except httpx.HTTPStatusError as exc:
            result = http_status_tool_error_result(
                exc,
                tool_name=tool_name or "unknown",
                trace_id=trace_id,
                elapsed_ms=round((perf_counter() - started) * 1000),
            )
        except Exception as exc:
            result = ToolCallResult(
                tool_name=tool_name or "unknown",
                status="error",
                error=safe_exception_summary(exc),
                idempotency_key=f"{trace_id}:{tool_name}",
                response={"elapsed_ms": round((perf_counter() - started) * 1000)},
                elapsed_ms=round(
                    (perf_counter() - started) * 1000
                ),
                retryable=isinstance(
                    exc,
                    BackendV13TransportError,
                ),
            )
        elapsed_ms = round((perf_counter() - started) * 1000)
        trace_logging.log_info(
            "agent_mcp_tools_call_completed",
            trace_id=trace_id,
            source_event_type=source_event_type,
            tool=result.tool_name,
            status=result.status,
            is_error=result.status == "error",
            elapsed_ms=elapsed_ms,
            error=trace_logging.snippet(result.error),
            idempotency_key_present=bool(result.idempotency_key),
        )
        return mcp_result_from_tool_result(result)

    async def _execute_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
        tool_call_id: str = "",
    ) -> ToolCallResult:
        if tool_name not in ALLOWED_TOOL_NAMES:
            return ToolCallResult(tool_name=tool_name or "unknown", status="error", error=f"unsupported_tool:{tool_name}")
        approved_key_call = self._is_approved_key_call(
            tool_name,
            arguments,
            payload,
        )
        denial_reason = (
            None
            if approved_key_call
            else validate_tool_permission(
                {"name": tool_name, "arguments": arguments},
                source_event_type=source_event_type,
                payload=payload,
            )
        )
        if denial_reason:
            return permission_denied_result(
                {"name": tool_name, "arguments": arguments},
                trace_id=trace_id,
                source_event_type=source_event_type,
                reason=denial_reason,
            )
        if (
            tool_name == CREATE_MEDICATION_SIDE_EFFECT_RECORD
            and not approved_key_call
        ):
            allowed_side_effect_arguments = {
                "symptom_text",
                "symptom_onset_text",
                "medication_name",
            }
            if set(arguments).difference(allowed_side_effect_arguments):
                return ToolCallResult(
                    tool_name=tool_name,
                    status="error",
                    error="unsupported_model_arguments",
                    idempotency_key=f"{trace_id}:{tool_name}",
                )
        if tool_name == REQUEST_RECORD_APPROVAL:
            return await self._request_record_approval(
                arguments,
                tool_call_id=tool_call_id,
                trace_id=trace_id,
                source_event_type=source_event_type,
                payload=payload,
            )
        if tool_name in RECORD_APPROVAL_ACTIONS:
            if not approved_key_call:
                return ToolCallResult(
                    tool_name=tool_name,
                    status="error",
                    error="record_approval_required",
                    response={
                        "contract_version": "v1.3",
                        "required_tool": REQUEST_RECORD_APPROVAL,
                    },
                    idempotency_key=(
                        f"{trace_id}:{tool_name}:approval_required"
                    ),
                )
            if tool_name == UPSERT_NUTRITION_PREFERENCE_FACT:
                return ToolCallResult(
                    tool_name=tool_name,
                    status="error",
                    error="agent_internal_preference_write_not_implemented",
                    idempotency_key=f"{trace_id}:{tool_name}:disabled",
                )
            if is_backend_v13_sync_write(tool_name):
                return await self._execute_confirmed_backend_write_v13(
                    tool_name,
                    approval_key=str(arguments.get("approval_key") or ""),
                    trace_id=trace_id,
                    source_event_type=source_event_type,
                    payload=payload,
                )
        if ConfirmationActionRegistry.requires_confirmation(tool_name):
            return ToolCallResult(
                tool_name=tool_name,
                status="error",
                error="backend_write_tool_not_supported",
                response={"contract_version": "v1.3"},
                idempotency_key=f"{trace_id}:{tool_name}:unsupported_write",
            )
        if tool_name in DEFERRED_POLICY_TOOL_NAMES:
            return deferred_policy_tool_result({"name": tool_name, "arguments": arguments}, trace_id=trace_id, source_event_type=source_event_type)
        if tool_name == GET_MEDICATION_DOSE_STATUS:
            return await self._get_medication_dose_status(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NOTIFICATION_POLICIES:
            return await self._get_notification_policies(arguments, trace_id=trace_id, payload=payload)
        if tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES:
            return await self._search_nutrition_food_candidates(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NUTRITION_MEAL_RECORD_LIST:
            return await self._get_nutrition_meal_record_list(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NUTRITION_DAILY_SUMMARY:
            return await self._get_nutrition_daily_summary(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NUTRITION_PREFERENCE_SUMMARY:
            return await self._get_nutrition_preference_summary(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NUTRITION_RECOMMENDATION_CANDIDATES:
            return await self._get_nutrition_recommendation_candidates(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT:
            return await self._get_medication_side_effect_assessment(
                arguments,
                trace_id=trace_id,
                source_event_type=source_event_type,
                payload=payload,
            )
        if tool_name == GET_SIDE_EFFECT_HISTORY:
            return await self._get_side_effect_history(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_PRO_CTCAE_QUESTIONNAIRE:
            return self._ae_pro_ctcae(arguments, trace_id=trace_id)
        return ToolCallResult(tool_name=tool_name or "unknown", status="error", error=f"unsupported_tool:{tool_name}")

    async def _execute_confirmed_backend_write_v13(
        self,
        tool_name: str,
        *,
        approval_key: str,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        request_metadata = context_value(payload, "request_metadata")
        request_metadata = (
            request_metadata if isinstance(request_metadata, dict) else {}
        )
        source_chat_request_id = str(
            request_metadata.get("request_id") or ""
        )
        confirmation_message_id = str(
            request_metadata.get("message_id") or ""
        )
        grant = await anyio.to_thread.run_sync(
            lambda: self.backend_writes.approval_store.resolve_grant(
                approval_key=approval_key,
                patient_id=str(payload.get("patient_id") or ""),
                action_name=tool_name,
                source_chat_request_id=source_chat_request_id,
                confirmation_message_id=confirmation_message_id,
            )
        )
        authoritative_arguments = dict(grant.arguments)
        allowed = MODEL_WRITE_ARGUMENTS.get(tool_name, frozenset())
        business_arguments = {
            key: authoritative_arguments[key]
            for key in allowed
            if key in authoritative_arguments
        }
        write_result = await self.backend_writes.execute(
            tool_name,
            business_arguments,
            tool_call_id=f"approved_{tool_name}",
            expected_version_override=grant.expected_version,
            authoritative_arguments_override=authoritative_arguments,
            context=BackendWriteInvocationContext(
                source_chat_request_id=grant.source_chat_request_id,
                source_message_id=grant.confirmation_message_id,
                approval_key=grant.approval_key,
                action_fingerprint=grant.action_fingerprint,
                patient_id=str(payload.get("patient_id") or ""),
                requested_at=payload.get("current_time"),
            ),
        )
        return write_result

    async def _request_record_approval(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        action_name = str(arguments.get("action_name") or "").strip()
        record_arguments = arguments.get("record_arguments")
        if (
            action_name not in RECORD_APPROVAL_ACTIONS
            or not isinstance(record_arguments, dict)
        ):
            return ToolCallResult(
                tool_name=REQUEST_RECORD_APPROVAL,
                status="error",
                error="record_approval_arguments_invalid",
                idempotency_key=f"{trace_id}:{REQUEST_RECORD_APPROVAL}",
            )
        record_arguments = _project_record_arguments_to_action_schema(
            action_name,
            record_arguments,
        )
        approval_display_context: dict[str, Any] = {
            "requested_at": payload.get("current_time"),
        }
        trusted_patient_context = context_value(
            payload,
            "trusted_patient_context",
        )
        if isinstance(trusted_patient_context, dict):
            approval_display_context[
                "trusted_patient_context"
            ] = trusted_patient_context
        if action_name == CREATE_MEDICATION_SIDE_EFFECT_RECORD:
            completed_survey = context_value(
                payload,
                "completed_pro_ctcae_survey",
            )
            if isinstance(completed_survey, dict):
                approval_display_context.update(completed_survey)
            try:
                record_arguments = self._authoritative_side_effect_record_arguments(
                    record_arguments,
                    trace_id=trace_id,
                    source_event_type=source_event_type,
                    payload=payload,
                )
            except ValueError as exc:
                if str(exc) == "pro_ctcae_survey_completion_required":
                    return ToolCallResult(
                        tool_name=REQUEST_RECORD_APPROVAL,
                        status="success",
                        response={
                            "approval_status": "survey_required",
                            "approval_created": False,
                            "record_applied": False,
                            "required_state": (
                                "completed_pro_ctcae_survey"
                            ),
                            "message": (
                                "먼저 PRO-CTCAE 설문을 완료해야 합니다."
                            ),
                        },
                        idempotency_key=(
                            f"{trace_id}:{REQUEST_RECORD_APPROVAL}:"
                            "survey_required"
                        ),
                    )
                return ToolCallResult(
                    tool_name=REQUEST_RECORD_APPROVAL,
                    status="error",
                    error=str(exc),
                    idempotency_key=f"{trace_id}:{REQUEST_RECORD_APPROVAL}",
                )
        return await self._prepare_internal_approval(
            action_name,
            record_arguments,
            tool_call_id=tool_call_id,
            trace_id=trace_id,
            source_event_type=source_event_type,
            payload=payload,
            result_tool_name=REQUEST_RECORD_APPROVAL,
            display_context=approval_display_context,
        )

    @staticmethod
    def _patient_id_for_tool(
        arguments: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        del arguments
        return payload.get("patient_id")

    @staticmethod
    def _authoritative_side_effect_record_arguments(
        arguments: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {"symptom_text", "symptom_onset_text", "medication_name"}
        unsupported = sorted(set(arguments).difference(allowed))
        if unsupported:
            raise ValueError("unsupported_model_arguments")
        patient_snapshot = context_value(payload, "trusted_patient_context")
        if not isinstance(patient_snapshot, dict):
            raise ValueError("patient_context_unavailable")
        availability = patient_snapshot.get("availability")
        if (
            isinstance(availability, dict)
            and availability.get("today_medication") == "unavailable"
        ):
            raise ValueError("patient_context_incomplete")
        symptom_text = str(arguments.get("symptom_text") or "").strip()
        if not symptom_text:
            raise ValueError("symptom_text_required")
        completed_survey = context_value(
            payload,
            "completed_pro_ctcae_survey",
        )
        if not isinstance(completed_survey, dict):
            raise ValueError("pro_ctcae_survey_completion_required")
        for key in (
            "symptom_text",
            "symptom_onset_text",
            "medication_name",
        ):
            supplied = str(arguments.get(key) or "").strip()
            completed_value = str(
                completed_survey.get(key) or ""
            ).strip()
            if supplied != completed_value:
                raise ValueError(
                    "pro_ctcae_survey_context_mismatch"
                )
        severity_raw = completed_survey.get("severity")
        if not isinstance(severity_raw, dict):
            raise ValueError("pro_ctcae_survey_severity_required")
        severity = ProCtcaeSeverityResult.model_validate(
            severity_raw
        )
        draft = side_effect_record_draft_from_snapshot(
            symptom_text=symptom_text,
            symptom_onset_text=str(
                arguments.get("symptom_onset_text") or ""
            ).strip(),
            medication_name=str(arguments.get("medication_name") or "").strip()
            or None,
            patient_snapshot=patient_snapshot,
            trace_id=trace_id,
            source_event_type=source_event_type,
        )
        draft["severity"] = severity.model_dump(mode="json")
        return draft

    async def _prepare_internal_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
        result_tool_name: str | None = None,
        display_context: dict[str, Any] | None = None,
    ) -> ToolCallResult:
        prepare_arguments = dict(arguments)
        request_metadata = context_value(payload, "request_metadata")
        request_metadata = (
            request_metadata if isinstance(request_metadata, dict) else {}
        )
        proposal = await anyio.to_thread.run_sync(
            lambda: self.backend_writes.approval_store.prepare(
                patient_id=str(payload.get("patient_id") or ""),
                source_chat_request_id=str(
                    request_metadata.get("request_id") or ""
                ),
                source_message_id=str(
                    request_metadata.get("message_id") or ""
                ),
                trace_id=trace_id,
                action_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=prepare_arguments,
                display=_approval_display(
                    tool_name,
                    prepare_arguments,
                    display_context=display_context,
                ),
            )
        )
        return ToolCallResult(
            tool_name=result_tool_name or tool_name,
            status="confirmation_required",
            response={
                "mutation_confirmation": proposal.public_payload()
            },
            idempotency_key=(
                f"{trace_id}:{tool_name}:{proposal.action_fingerprint}"
            ),
        )

    @staticmethod
    def _is_approved_key_call(
        tool_name: str,
        arguments: dict[str, Any],
        payload: dict[str, Any],
    ) -> bool:
        if tool_name not in RECORD_APPROVAL_ACTIONS:
            return False
        approved = context_value(payload, "approved_user_action")
        if not isinstance(approved, dict):
            return False
        if approved.get("status") != "confirmed":
            return False
        if str(approved.get("action_name") or "") != tool_name:
            return False
        approved_arguments = approved.get("arguments")
        return (
            isinstance(approved_arguments, dict)
            and approved_arguments == arguments
            and set(arguments) == {"approval_key"}
            and bool(str(arguments.get("approval_key") or "").strip())
        )

    async def _get_medication_dose_status(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = self._patient_id_for_tool(arguments, payload)
        if patient_id:
            params["patient_id"] = patient_id
        for key in ("target_date", "start_date", "end_date", "status", "medication_name"):
            if arguments.get(key):
                params[key] = arguments[key]
        if not any(
            params.get(key)
            for key in ("target_date", "start_date", "end_date")
        ):
            current_date = _payload_current_date(payload)
            if current_date is not None:
                params["target_date"] = current_date
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                GET_MEDICATION_DOSE_STATUS,
                trace_id=trace_id,
            )
        direct_result = await anyio.to_thread.run_sync(
            lambda: self.backend_queries.medication_dose_status(**params)
        )
        result = MedicationDoseStatusResult.model_validate(direct_result)
        return ToolCallResult(
            tool_name=GET_MEDICATION_DOSE_STATUS,
            status="success" if result.success else "error",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:{GET_MEDICATION_DOSE_STATUS}",
        )

    async def _get_notification_policies(
        self,
        arguments: dict[str, Any],
        *,
        trace_id: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                GET_NOTIFICATION_POLICIES,
                trace_id=trace_id,
            )
        params: dict[str, Any] = {
            "patient_id": str(payload.get("patient_id") or ""),
            "active_only": arguments.get("active_only", True),
        }
        for key in ("policy_id", "slot_label"):
            if arguments.get(key):
                params[key] = arguments[key]
        result = await anyio.to_thread.run_sync(
            lambda: self.backend_queries.notification_policies(**params)
        )
        return ToolCallResult(
            tool_name=GET_NOTIFICATION_POLICIES,
            status="success",
            response=result,
            idempotency_key=f"{trace_id}:{GET_NOTIFICATION_POLICIES}",
        )

    async def _search_nutrition_food_candidates(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            "query": arguments.get("query", ""),
            "limit": arguments.get("limit", 6),
            "patient_id": self._patient_id_for_tool(arguments, payload),
            "meal_type": arguments.get("meal_type"),
        }
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                SEARCH_NUTRITION_FOOD_CANDIDATES,
                trace_id=trace_id,
            )
        result = await anyio.to_thread.run_sync(
            lambda: self.backend_queries.search_food_candidates(
                query=str(request_payload["query"]),
                limit=int(request_payload["limit"]),
            )
        )
        return ToolCallResult(
            tool_name=SEARCH_NUTRITION_FOOD_CANDIDATES,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{SEARCH_NUTRITION_FOOD_CANDIDATES}",
        )

    async def _get_nutrition_meal_record_list(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = self._patient_id_for_tool(arguments, payload)
        if patient_id:
            params["patient_id"] = patient_id
        if arguments.get("meal_date"):
            params["meal_date"] = arguments["meal_date"]
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                GET_NUTRITION_MEAL_RECORD_LIST,
                trace_id=trace_id,
            )
        result = await anyio.to_thread.run_sync(
            lambda: self.backend_queries.nutrition_meals(**params)
        )
        return ToolCallResult(
            tool_name=GET_NUTRITION_MEAL_RECORD_LIST,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{GET_NUTRITION_MEAL_RECORD_LIST}",
        )

    async def _get_nutrition_daily_summary(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = self._patient_id_for_tool(arguments, payload)
        if patient_id:
            params["patient_id"] = patient_id
        if arguments.get("meal_date"):
            params["meal_date"] = arguments["meal_date"]
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                GET_NUTRITION_DAILY_SUMMARY,
                trace_id=trace_id,
            )
        result = await anyio.to_thread.run_sync(
            lambda: self.backend_queries.daily_nutrition_summary(**params)
        )
        return ToolCallResult(
            tool_name=GET_NUTRITION_DAILY_SUMMARY,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{GET_NUTRITION_DAILY_SUMMARY}",
        )

    async def _get_nutrition_preference_summary(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = self._patient_id_for_tool(arguments, payload)
        if patient_id:
            params["patient_id"] = patient_id
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                GET_NUTRITION_PREFERENCE_SUMMARY,
                trace_id=trace_id,
            )
        result = await anyio.to_thread.run_sync(
            lambda: self.backend_queries.nutrition_preferences(**params)
        )
        return ToolCallResult(
            tool_name=GET_NUTRITION_PREFERENCE_SUMMARY,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{GET_NUTRITION_PREFERENCE_SUMMARY}",
        )

    async def _get_nutrition_recommendation_candidates(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            "patient_id": self._patient_id_for_tool(arguments, payload),
            "constraints": arguments.get("constraints") or {},
            "meal_type": arguments.get("meal_type"),
            "limit": arguments.get("limit", 5),
            "randomize": arguments.get("randomize", True),
        }
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                GET_NUTRITION_RECOMMENDATION_CANDIDATES,
                trace_id=trace_id,
            )
        result = await anyio.to_thread.run_sync(
            lambda: self.backend_queries.recommendation_candidates(
                patient_id=str(request_payload["patient_id"] or ""),
                limit=int(request_payload["limit"]),
            )
        )
        return ToolCallResult(
            tool_name=GET_NUTRITION_RECOMMENDATION_CANDIDATES,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{GET_NUTRITION_RECOMMENDATION_CANDIDATES}",
        )

    async def _get_medication_side_effect_assessment(
        self,
        arguments: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        patient_snapshot = context_value(payload, "trusted_patient_context")
        if not isinstance(patient_snapshot, dict):
            return ToolCallResult(
                tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                status="error",
                error="patient_context_unavailable",
                idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
            )
        availability = patient_snapshot.get("availability")
        medication_availability = (
            availability.get("today_medication")
            if isinstance(availability, dict)
            else None
        )
        if medication_availability == "unavailable":
            return ToolCallResult(
                tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                status="error",
                error="patient_context_incomplete",
                idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
            )
        symptom_text = str(arguments.get("symptom_text") or "").strip()
        if not symptom_text:
            return ToolCallResult(
                tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                status="error",
                error="symptom_text_required",
                idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
            )
        medication_name = str(
            arguments.get("medication_name")
            or payload.get("medication_name")
            or context_value(payload, "medication_name")
            or ""
        ).strip()
        symptom_onset_text = str(
            arguments.get("symptom_onset_text") or ""
        ).strip()
        try:
            result = assess_side_effect_from_snapshot(
                symptom_text=symptom_text,
                patient_snapshot=patient_snapshot,
                medication_name=medication_name or None,
            )
        except ValueError as exc:
            return ToolCallResult(
                tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                status="error",
                error=str(exc),
                idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
            )
        response_payload = result.model_dump(mode="json")
        response_payload["side_effect_record_draft"] = (
            side_effect_record_draft_from_snapshot(
                symptom_text=symptom_text,
                symptom_onset_text=symptom_onset_text,
                medication_name=medication_name or None,
                patient_snapshot=patient_snapshot,
                trace_id=trace_id,
                source_event_type=source_event_type,
            )
        )
        return ToolCallResult(
            tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
            status="success",
            response=response_payload,
            idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
        )

    async def _get_side_effect_history(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = self._patient_id_for_tool(arguments, payload)
        if patient_id:
            params["patient_id"] = patient_id
        if arguments.get("limit") is not None:
            params["limit"] = arguments["limit"]
        if arguments.get("suspected") is not None:
            params["suspected"] = arguments["suspected"]
        for key in (
            "target_date",
            "start_date",
            "end_date",
            "medication_name",
        ):
            if arguments.get(key):
                params[key] = arguments[key]
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                GET_SIDE_EFFECT_HISTORY,
                trace_id=trace_id,
            )
        direct_params = {
            key: value
            for key, value in params.items()
            if key
            in {
                "patient_id",
                "limit",
                "suspected",
                "target_date",
                "start_date",
                "end_date",
                "medication_name",
            }
        }
        result = SideEffectHistoryResult.model_validate(
            await anyio.to_thread.run_sync(
                lambda: self.backend_queries.side_effect_history(**direct_params)
            )
        )
        return ToolCallResult(
            tool_name=GET_SIDE_EFFECT_HISTORY,
            status="success" if result.success else "error",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:{GET_SIDE_EFFECT_HISTORY}",
        )

    @staticmethod
    def _ae_pro_ctcae(arguments: dict[str, Any], *, trace_id: str) -> ToolCallResult:
        request = AEProCtcaeAssessmentRequest.model_validate(arguments)
        try:
            result = match_pro_ctcae_symptom(request.symptom_text)
        except ProCtcaeReferenceUnavailable as exc:
            return ToolCallResult(
                tool_name=GET_PRO_CTCAE_QUESTIONNAIRE,
                status="error",
                error=str(exc),
                idempotency_key=(
                    f"{trace_id}:{GET_PRO_CTCAE_QUESTIONNAIRE}"
                ),
            )
        return ToolCallResult(
            tool_name=GET_PRO_CTCAE_QUESTIONNAIRE,
            status="success",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:{GET_PRO_CTCAE_QUESTIONNAIRE}",
        )


def _payload_current_date(payload: dict[str, Any]) -> str | None:
    value = payload.get("current_time")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None


def backend_read_unavailable_result(
    tool_name: str,
    *,
    trace_id: str,
) -> ToolCallResult:
    return ToolCallResult(
        tool_name=tool_name,
        status="error",
        error="backend_read_query_tools_not_configured",
        idempotency_key=f"{trace_id}:{tool_name}",
    )


def http_status_tool_error_result(
    exc: httpx.HTTPStatusError,
    *,
    tool_name: str,
    trace_id: str,
    elapsed_ms: int,
) -> ToolCallResult:
    detail = _http_error_detail(exc.response)
    error_code = safe_tool_error(detail or f"http_status_{exc.response.status_code}")
    return ToolCallResult(
        tool_name=tool_name or "unknown",
        status="error",
        error=error_code,
        response={
            "status_code": exc.response.status_code,
            "detail": error_code,
            "elapsed_ms": elapsed_ms,
        },
        idempotency_key=f"{trace_id}:{tool_name or 'unknown'}",
        elapsed_ms=elapsed_ms,
        retryable=exc.response.status_code in {429, 503, 504},
    )


def _http_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"]
    return ""
