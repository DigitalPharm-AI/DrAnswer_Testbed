from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx
import anyio

from agent_app import trace_logging
from agent_app.ae_pro_ctcae import match_pro_ctcae_symptom
from agent_app.integration.backend_client import BackendV12Client
from agent_app.integration.mutations import ConfirmedMutationContext, record_change_request_from_tool
from agent_app.orchestration.confirmations import ConfirmationActionRegistry
from agent_app.llm.context import context_value
from agent_app.tools.catalog import ToolCatalog
from agent_app.tools.backend_query import BackendQueryTools
from agent_app.tools.backend_write import (
    BackendSyncWriteTools,
    BackendWriteInvocationContext,
    is_backend_v12_sync_write,
)
from agent_app.tools.names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NOTIFICATION_POLICIES,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from agent_app.tools.permissions import allowed_tool_names_for_source, permission_denied_result, validate_tool_permission
from agent_app.tools.policy import DEFERRED_POLICY_TOOL_NAMES, deferred_policy_tool_result
from agent_app.tools.protocol import (
    ALLOWED_TOOL_NAMES,
    MCP_METHOD_TOOLS_CALL,
    MCP_METHOD_TOOLS_LIST,
    mcp_error_response,
    mcp_result_from_tool_result,
    mcp_success_response,
    mcp_tools_list,
)
from shared.redaction import redacted_clinical_text_label, safe_exception_summary
from shared.schemas import (
    AEProCtcaeAssessmentRequest,
    ConfirmedMutationExecutionRequest,
    ConfirmedMutationExecutionResult,
    DoseTakenToolRequest,
    DoseTakenToolResult,
    MedicationDoseStatusResult,
    MutationConfirmationPrepareRequest,
    MutationConfirmationPrepareResult,
    NutritionPreferenceFactRequest,
    SideEffectAssessmentRequest,
    SideEffectAssessmentResult,
    SideEffectHistoryResult,
    ToolCallResult,
)
from shared.settings import get_settings

JSON_RPC_PARSE_ERROR = -32700
JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601
JSON_RPC_INVALID_PARAMS = -32602


class AgentMcpToolServer:
    def __init__(
        self,
        *,
        system_base_url: str | None = None,
        phr_base_url: str | None = None,
        timeout_seconds: float = 30.0,
        backend_queries: BackendQueryTools | None = None,
        backend_client: BackendV12Client | None = None,
    ) -> None:
        settings = get_settings()
        self.system_base_url = (system_base_url or settings.system_base_url).rstrip("/")
        self.phr_base_url = (phr_base_url or settings.phr_base_url).rstrip("/")
        self.internal_api_token = settings.internal_api_token
        self.timeout_seconds = timeout_seconds
        self.backend_queries = backend_queries or BackendQueryTools.from_settings(settings)
        self.backend_client = backend_client or BackendV12Client.from_settings()
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
        denial_reason = validate_tool_permission(
            {"name": tool_name, "arguments": arguments},
            source_event_type=source_event_type,
            payload=payload,
        )
        if denial_reason:
            return permission_denied_result(
                {"name": tool_name, "arguments": arguments},
                trace_id=trace_id,
                source_event_type=source_event_type,
                reason=denial_reason,
            )
        if self._is_v12_chat(payload):
            if is_backend_v12_sync_write(tool_name):
                return await self._execute_backend_write_v12(
                    tool_name,
                    arguments,
                    tool_call_id=tool_call_id,
                    payload=payload,
                )
            if ConfirmationActionRegistry.requires_confirmation(tool_name):
                return ToolCallResult(
                    tool_name=tool_name,
                    status="error",
                    error="backend_v12_write_tool_not_supported",
                    response={"contract_version": "v1.2"},
                    idempotency_key=f"{trace_id}:{tool_name}:unsupported_v12_write",
                )
        if tool_name in DEFERRED_POLICY_TOOL_NAMES:
            return deferred_policy_tool_result({"name": tool_name, "arguments": arguments}, trace_id=trace_id, source_event_type=source_event_type)
        if ConfirmationActionRegistry.requires_confirmation(tool_name):
            approved = context_value(payload, "approved_mutation_confirmation")
            if isinstance(approved, dict) and approved.get("confirmation_id"):
                if approved.get("confirmation_message_id"):
                    return await self._execute_confirmed_mutation_v12(
                        tool_name,
                        arguments,
                        approved,
                        trace_id=trace_id,
                        payload=payload,
                    )
                return await self._execute_confirmed_mutation(
                    tool_name,
                    approved,
                    trace_id=trace_id,
                    source_event_type=source_event_type,
                )
            return await self._prepare_mutation_confirmation(
                tool_name,
                arguments,
                tool_call_id=tool_call_id,
                trace_id=trace_id,
                source_event_type=source_event_type,
                payload=payload,
            )
        if tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
            return await self._mark_dose_taken(arguments, trace_id=trace_id, source_event_type=source_event_type)
        if tool_name == GET_MEDICATION_DOSE_STATUS:
            return await self._get_medication_dose_status(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NOTIFICATION_POLICIES:
            return await self._get_notification_policies(arguments, trace_id=trace_id, payload=payload)
        if tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES:
            return await self._search_food_nutrition(arguments, trace_id=trace_id, payload=payload)
        if tool_name == CREATE_NUTRITION_MEAL_RECORD:
            return await self._record_meal(arguments, trace_id=trace_id, payload=payload)
        if tool_name == UPDATE_NUTRITION_MEAL_RECORD:
            return await self._update_nutrition_meal(arguments, trace_id=trace_id, payload=payload)
        if tool_name == DELETE_NUTRITION_MEAL_RECORD:
            return await self._delete_nutrition_meal(arguments, trace_id=trace_id, payload=payload)
        if tool_name == UPDATE_NUTRITION_FOOD_RECORD:
            return await self._update_nutrition_food(arguments, trace_id=trace_id, payload=payload)
        if tool_name == DELETE_NUTRITION_FOOD_RECORD:
            return await self._delete_nutrition_food(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NUTRITION_MEAL_RECORD_LIST:
            return await self._list_meals(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NUTRITION_DAILY_SUMMARY:
            return await self._get_daily_nutrition_summary(arguments, trace_id=trace_id, payload=payload)
        if tool_name == UPSERT_NUTRITION_PREFERENCE_FACT:
            return await self._record_nutrition_preference(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NUTRITION_PREFERENCE_SUMMARY:
            return await self._get_nutrition_preferences(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_NUTRITION_RECOMMENDATION_CANDIDATES:
            return await self._recommend_diet(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT:
            return await self._lookup_side_effect_info(arguments, trace_id=trace_id, source_event_type=source_event_type, payload=payload)
        if tool_name == GET_SIDE_EFFECT_HISTORY:
            return await self._get_side_effect_history(arguments, trace_id=trace_id, payload=payload)
        if tool_name == GET_PRO_CTCAE_QUESTIONNAIRE:
            return self._ae_pro_ctcae(arguments, trace_id=trace_id)
        return ToolCallResult(tool_name=tool_name or "unknown", status="error", error=f"unsupported_tool:{tool_name}")

    async def _execute_backend_write_v12(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        request_metadata = context_value(payload, "request_metadata")
        request_metadata = request_metadata if isinstance(request_metadata, dict) else {}
        return await self.backend_writes.execute(
            tool_name,
            arguments,
            tool_call_id=tool_call_id,
            context=BackendWriteInvocationContext(
                source_chat_request_id=str(request_metadata.get("request_id") or ""),
                source_message_id=str(request_metadata.get("message_id") or ""),
                conversation_id=str(request_metadata.get("conversation_id") or ""),
                patient_id=str(payload.get("patient_id") or ""),
                requested_at=payload.get("current_time"),
            ),
        )

    @staticmethod
    def _is_v12_chat(payload: dict[str, Any]) -> bool:
        request_metadata = context_value(payload, "request_metadata")
        return (
            isinstance(request_metadata, dict)
            and request_metadata.get("contract_version") == "v1.2"
        )

    async def _prepare_mutation_confirmation(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        request = MutationConfirmationPrepareRequest(
            patient_id=str(payload.get("patient_id") or ""),
            action_type="agent_tool",
            action_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
            trace_id=trace_id,
            source_event_type=source_event_type,
            request_context={
                "message": payload.get("message"),
                "event_type": payload.get("event_type"),
                "current_time": payload.get("current_time"),
                "callback_context": payload.get("callback_context"),
                "request_metadata": context_value(payload, "request_metadata"),
                "mutation_resolution": context_value(payload, "mutation_resolution"),
            },
        )
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/mutation-confirmations/prepare",
                json=request.model_dump(mode="json"),
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = MutationConfirmationPrepareResult.model_validate(response.json())
        if not result.confirmation_required:
            already_applied = result.status == "already_applied"
            return ToolCallResult(
                tool_name=tool_name,
                status="success" if already_applied else "error",
                response=result.execution_result,
                error="" if already_applied else f"mutation_confirmation_{result.status}",
                idempotency_key=f"{trace_id}:{tool_name}:{result.action_fingerprint}",
            )
        return ToolCallResult(
            tool_name=tool_name,
            status="confirmation_required",
            response={"mutation_confirmation": result.model_dump(mode="json")},
            idempotency_key=f"{trace_id}:{tool_name}:{result.action_fingerprint}",
        )

    async def _execute_confirmed_mutation(
        self,
        tool_name: str,
        approved: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
    ) -> ToolCallResult:
        request = ConfirmedMutationExecutionRequest(
            confirmation_id=str(approved.get("confirmation_id") or ""),
            action_name=tool_name,
            action_fingerprint=str(approved.get("action_fingerprint") or ""),
            trace_id=trace_id,
            source_event_type=source_event_type,
        )
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/mutation-confirmations/{request.confirmation_id}/execute",
                json=request.model_dump(mode="json"),
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = ConfirmedMutationExecutionResult.model_validate(response.json())
        return ToolCallResult(
            tool_name=tool_name,
            status="success" if result.status == "applied" else "error",
            response=result.tool_result,
            error=result.error or ("" if result.status == "applied" else f"mutation_confirmation_{result.status}"),
            idempotency_key=f"{request.confirmation_id}:{tool_name}:confirmed",
        )

    async def _execute_confirmed_mutation_v12(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        approved: dict[str, Any],
        *,
        trace_id: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        request_metadata = context_value(payload, "request_metadata")
        request_metadata = request_metadata if isinstance(request_metadata, dict) else {}
        context = ConfirmedMutationContext(
            request_id=str(approved.get("write_request_id") or approved.get("request_id") or f"{trace_id}:{tool_name}"),
            source_chat_request_id=str(
                approved.get("source_chat_request_id")
                or request_metadata.get("source_chat_request_id")
                or request_metadata.get("request_id")
                or ""
            ),
            conversation_id=str(
                approved.get("conversation_id")
                or request_metadata.get("conversation_id")
                or ""
            ),
            confirmation_message_id=str(approved.get("confirmation_message_id") or ""),
            patient_id=str(payload.get("patient_id") or ""),
            requested_at=payload.get("current_time"),
        )
        request = record_change_request_from_tool(tool_name, arguments, context)
        response = await self.backend_client.change_record(request)
        response_body = response.model_dump(mode="json")
        return ToolCallResult(
            tool_name=tool_name,
            status="success" if response.success else "error",
            response=response_body,
            error=response.error.code if response.error else "",
            idempotency_key=request.request_id,
        )

    def _internal_headers(self) -> dict[str, str]:
        if not self.internal_api_token:
            return {}
        return {"X-Internal-Api-Token": self.internal_api_token}

    async def _mark_dose_taken(self, arguments: dict[str, Any], *, trace_id: str, source_event_type: str) -> ToolCallResult:
        payload = dict(arguments)
        payload["source_trace_id"] = trace_id
        payload["source_event_type"] = source_event_type
        request = DoseTakenToolRequest.model_validate(payload)
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/dose-events/mark-taken",
                json=request.model_dump(mode="json"),
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = DoseTakenToolResult.model_validate(response.json())
        return ToolCallResult(
            tool_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
            status="success" if result.status == "taken" else "error",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:{UPDATE_MEDICATION_DOSE_EVENT_STATUS}:{source_event_type}:{request.dose_event_id}",
        )

    async def _get_medication_dose_status(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = arguments.get("patient_id") or payload.get("patient_id")
        if patient_id:
            params["patient_id"] = patient_id
        for key in ("target_date", "start_date", "end_date", "status", "medication_name"):
            if arguments.get(key):
                params[key] = arguments[key]
        if self.backend_queries is not None:
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
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.get(
                f"{self.system_base_url}/api/agent/dose-events",
                params=params,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = MedicationDoseStatusResult.model_validate(response.json())
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
            return ToolCallResult(
                tool_name=GET_NOTIFICATION_POLICIES,
                status="error",
                error="backend_read_query_tools_not_configured",
                idempotency_key=f"{trace_id}:{GET_NOTIFICATION_POLICIES}",
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

    async def _search_food_nutrition(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            "query": arguments.get("query", ""),
            "limit": arguments.get("limit", 6),
            "patient_id": arguments.get("patient_id") or payload.get("patient_id"),
            "meal_type": arguments.get("meal_type"),
        }
        if self.backend_queries is not None:
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
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/food/search",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        payload = response.json()
        return ToolCallResult(
            tool_name=SEARCH_NUTRITION_FOOD_CANDIDATES,
            status="success" if payload.get("success") else "error",
            response=payload,
            error=safe_tool_error(payload.get("error")),
            idempotency_key=f"{trace_id}:{SEARCH_NUTRITION_FOOD_CANDIDATES}",
        )

    async def _record_meal(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            **arguments,
            "patient_id": arguments.get("patient_id") or payload.get("patient_id"),
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/meals",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=CREATE_NUTRITION_MEAL_RECORD,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{CREATE_NUTRITION_MEAL_RECORD}:{result.get('meal', {}).get('id', 'unknown')}",
        )

    async def _update_nutrition_meal(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        meal_id = arguments.get("meal_id")
        request_payload = {key: value for key, value in arguments.items() if key != "meal_id" and value is not None}
        request_payload["patient_id"] = arguments.get("patient_id") or payload.get("patient_id")
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/meals/{meal_id}/update",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=UPDATE_NUTRITION_MEAL_RECORD,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{UPDATE_NUTRITION_MEAL_RECORD}:{meal_id}",
        )

    async def _delete_nutrition_meal(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        meal_id = arguments.get("meal_id")
        request_payload = {
            "patient_id": arguments.get("patient_id") or payload.get("patient_id"),
            "reason": arguments.get("reason", ""),
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/meals/{meal_id}/delete",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=DELETE_NUTRITION_MEAL_RECORD,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{DELETE_NUTRITION_MEAL_RECORD}:{meal_id}",
        )

    async def _update_nutrition_food(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        meal_id = arguments.get("meal_id")
        food_id = arguments.get("food_id")
        request_payload = {key: value for key, value in arguments.items() if key not in {"meal_id", "food_id"} and value is not None}
        request_payload["patient_id"] = arguments.get("patient_id") or payload.get("patient_id")
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/meals/{meal_id}/foods/{food_id}/update",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=UPDATE_NUTRITION_FOOD_RECORD,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{UPDATE_NUTRITION_FOOD_RECORD}:{meal_id}:{food_id}",
        )

    async def _delete_nutrition_food(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        meal_id = arguments.get("meal_id")
        food_id = arguments.get("food_id")
        request_payload = {
            "patient_id": arguments.get("patient_id") or payload.get("patient_id"),
            "reason": arguments.get("reason", ""),
            "delete_empty_meal": arguments.get("delete_empty_meal", True),
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/meals/{meal_id}/foods/{food_id}/delete",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=DELETE_NUTRITION_FOOD_RECORD,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{DELETE_NUTRITION_FOOD_RECORD}:{meal_id}:{food_id}",
        )

    async def _list_meals(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = arguments.get("patient_id") or payload.get("patient_id")
        if patient_id:
            params["patient_id"] = patient_id
        if arguments.get("meal_date"):
            params["meal_date"] = arguments["meal_date"]
        if self.backend_queries is not None:
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
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.get(
                f"{self.system_base_url}/api/agent/nutrition/meals",
                params=params,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=GET_NUTRITION_MEAL_RECORD_LIST,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{GET_NUTRITION_MEAL_RECORD_LIST}",
        )

    async def _get_daily_nutrition_summary(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = arguments.get("patient_id") or payload.get("patient_id")
        if patient_id:
            params["patient_id"] = patient_id
        if arguments.get("meal_date"):
            params["meal_date"] = arguments["meal_date"]
        if self.backend_queries is not None:
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
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.get(
                f"{self.system_base_url}/api/agent/nutrition/daily-summary",
                params=params,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=GET_NUTRITION_DAILY_SUMMARY,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{GET_NUTRITION_DAILY_SUMMARY}",
        )

    async def _record_nutrition_preference(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            **arguments,
            "patient_id": arguments.get("patient_id") or payload.get("patient_id"),
            "source_trace_id": arguments.get("source_trace_id") or trace_id,
        }
        request = NutritionPreferenceFactRequest.model_validate(request_payload)
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/preferences/facts",
                json=request.model_dump(mode="json"),
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        fact = result.get("fact") if isinstance(result.get("fact"), dict) else {}
        return ToolCallResult(
            tool_name=UPSERT_NUTRITION_PREFERENCE_FACT,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{UPSERT_NUTRITION_PREFERENCE_FACT}:{fact.get('predicate', 'unknown')}:{fact.get('object_key', 'unknown')}",
        )

    async def _get_nutrition_preferences(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = arguments.get("patient_id") or payload.get("patient_id")
        if patient_id:
            params["patient_id"] = patient_id
        if self.backend_queries is not None:
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
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.get(
                f"{self.system_base_url}/api/agent/nutrition/preferences",
                params=params,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=GET_NUTRITION_PREFERENCE_SUMMARY,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{GET_NUTRITION_PREFERENCE_SUMMARY}",
        )

    async def _recommend_diet(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            "patient_id": arguments.get("patient_id") or payload.get("patient_id"),
            "constraints": arguments.get("constraints") or {},
            "meal_type": arguments.get("meal_type"),
            "limit": arguments.get("limit", 5),
            "randomize": arguments.get("randomize", True),
        }
        if self.backend_queries is not None:
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
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/recommend",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name=GET_NUTRITION_RECOMMENDATION_CANDIDATES,
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:{GET_NUTRITION_RECOMMENDATION_CANDIDATES}",
        )

    async def _lookup_side_effect_info(
        self,
        arguments: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
        request_payload = {
            **arguments,
            "phr_patient_key": arguments.get("phr_patient_key") or payload.get("phr_patient_key"),
            "medication_name": arguments.get("medication_name") or payload.get("medication_name") or context_value(payload, "medication_name"),
            "recent_chat": arguments.get("recent_chat") or payload.get("chat_context") or payload.get("conversation_context") or context_value(payload, "recent_chat") or [],
            "dose_event_id": arguments.get("dose_event_id") or payload.get("dose_event_id") or context_value(payload, "related_dose_event_id"),
        }
        if not request_payload.get("phr_patient_key"):
            return ToolCallResult(
                tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                status="skipped",
                error="phr_registration_required",
                idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
            )
        request = SideEffectAssessmentRequest.model_validate(request_payload)
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(f"{self.phr_base_url}/phr/side-effects/assess", json=request.model_dump(mode="json"))
            response.raise_for_status()
            result = SideEffectAssessmentResult.model_validate(response.json())
        response_payload = result.model_dump(mode="json")
        matched_items = list(result.matched_items)
        medication_name = request.medication_name or (matched_items[0] if len(matched_items) == 1 else "")
        response_payload["side_effect_record_draft"] = {
            "phr_patient_key": request.phr_patient_key,
            "medication_name": medication_name,
            "symptom_text": request.symptom_text,
            "suspected": result.suspected,
            "severity": result.severity,
            "matched_effects": result.matched_effects,
            "matched_items": result.matched_items,
            "evidence": result.evidence,
            "recommendation": result.recommendation,
            "source_trace_id": trace_id,
            "source_event_type": source_event_type,
            "related_dose_event_id": request.dose_event_id,
            "metadata": {"source_tool": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT},
        }
        return ToolCallResult(
            tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
            status="success",
            response=response_payload,
            idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
        )

    async def _get_side_effect_history(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = arguments.get("patient_id") or payload.get("patient_id")
        if patient_id:
            params["patient_id"] = patient_id
        if arguments.get("limit") is not None:
            params["limit"] = arguments["limit"]
        if arguments.get("suspected") is not None:
            params["suspected"] = arguments["suspected"]
        for key in ("target_date", "start_date", "end_date", "medication_name", "severity"):
            if arguments.get(key):
                params[key] = arguments[key]
        if self.backend_queries is not None:
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
                    "severity",
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
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.get(
                f"{self.system_base_url}/api/agent/side-effects/history",
                params=params,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = SideEffectHistoryResult.model_validate(response.json())
        return ToolCallResult(
            tool_name=GET_SIDE_EFFECT_HISTORY,
            status="success" if result.success else "error",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:{GET_SIDE_EFFECT_HISTORY}",
        )

    @staticmethod
    def _ae_pro_ctcae(arguments: dict[str, Any], *, trace_id: str) -> ToolCallResult:
        request = AEProCtcaeAssessmentRequest.model_validate(arguments)
        result = match_pro_ctcae_symptom(request.symptom_normalize or request.symptom_text, threshold=request.threshold)
        return ToolCallResult(
            tool_name=GET_PRO_CTCAE_QUESTIONNAIRE,
            status="success",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:{GET_PRO_CTCAE_QUESTIONNAIRE}",
        )


def safe_tool_error(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 80 and all(char.isascii() and (char.isalnum() or char in "_:-.") for char in text):
        return text
    return redacted_clinical_text_label(text, key="error")


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
    )


def _http_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"]
    return ""
