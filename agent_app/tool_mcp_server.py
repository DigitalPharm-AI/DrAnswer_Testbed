from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from agent_app import trace_logging
from agent_app.ae_pro_ctcae import match_pro_ctcae_symptom
from agent_app.payload_context import context_value
from agent_app.tool_catalog import ToolCatalog
from agent_app.tool_permissions import allowed_tool_names_for_source, permission_denied_result, validate_tool_permission
from agent_app.tool_policy import DEFERRED_POLICY_TOOL_NAMES, deferred_policy_tool_result
from agent_app.tool_protocol import (
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
    DoseTakenToolRequest,
    DoseTakenToolResult,
    NutritionPreferenceFactRequest,
    SideEffectAssessmentRequest,
    SideEffectAssessmentResult,
    ToolCallResult,
)
from shared.settings import get_settings

JSON_RPC_PARSE_ERROR = -32700
JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601
JSON_RPC_INVALID_PARAMS = -32602


class AgentMcpToolServer:
    def __init__(self, *, system_base_url: str | None = None, phr_base_url: str | None = None, timeout_seconds: float = 30.0) -> None:
        settings = get_settings()
        self.system_base_url = (system_base_url or settings.system_base_url).rstrip("/")
        self.phr_base_url = (phr_base_url or settings.phr_base_url).rstrip("/")
        self.internal_api_token = settings.internal_api_token
        self.timeout_seconds = timeout_seconds

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
        started = perf_counter()
        trace_logging.log_info(
            "agent_mcp_tools_call_started",
            trace_id=trace_id,
            source_event_type=source_event_type,
            tool=tool_name,
            argument_keys=sorted(str(key) for key in arguments.keys()),
        )
        try:
            result = await self._execute_tool_result(tool_name, arguments, trace_id=trace_id, source_event_type=source_event_type, payload=payload)
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
        if tool_name in DEFERRED_POLICY_TOOL_NAMES:
            return deferred_policy_tool_result({"name": tool_name, "arguments": arguments}, trace_id=trace_id, source_event_type=source_event_type)
        if tool_name == "mark_dose_taken":
            return await self._mark_dose_taken(arguments, trace_id=trace_id, source_event_type=source_event_type)
        if tool_name == "search_food_nutrition":
            return await self._search_food_nutrition(arguments, trace_id=trace_id, payload=payload)
        if tool_name == "record_meal":
            return await self._record_meal(arguments, trace_id=trace_id, payload=payload)
        if tool_name == "list_meals":
            return await self._list_meals(arguments, trace_id=trace_id, payload=payload)
        if tool_name == "get_daily_nutrition_summary":
            return await self._get_daily_nutrition_summary(arguments, trace_id=trace_id, payload=payload)
        if tool_name == "record_nutrition_preference":
            return await self._record_nutrition_preference(arguments, trace_id=trace_id, payload=payload)
        if tool_name == "get_nutrition_preferences":
            return await self._get_nutrition_preferences(arguments, trace_id=trace_id, payload=payload)
        if tool_name == "recommend_diet":
            return await self._recommend_diet(arguments, trace_id=trace_id, payload=payload)
        if tool_name == "lookup_side_effect_info":
            return await self._lookup_side_effect_info(arguments, trace_id=trace_id, payload=payload)
        if tool_name == "AE_pro_ctcae":
            return self._ae_pro_ctcae(arguments, trace_id=trace_id)
        return ToolCallResult(tool_name=tool_name or "unknown", status="error", error=f"unsupported_tool:{tool_name}")

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
            tool_name="mark_dose_taken",
            status="success" if result.status == "taken" else "error",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:mark_dose_taken:{source_event_type}:{request.dose_event_id}",
        )

    async def _search_food_nutrition(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            "query": arguments.get("query", ""),
            "limit": arguments.get("limit", 10),
            "patient_id": arguments.get("patient_id") or payload.get("patient_id"),
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/food/search",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        payload = response.json()
        return ToolCallResult(
            tool_name="search_food_nutrition",
            status="success" if payload.get("success") else "error",
            response=payload,
            error=safe_tool_error(payload.get("error")),
            idempotency_key=f"{trace_id}:search_food_nutrition",
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
            tool_name="record_meal",
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:record_meal:{result.get('meal', {}).get('id', 'unknown')}",
        )

    async def _list_meals(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = arguments.get("patient_id") or payload.get("patient_id")
        if patient_id:
            params["patient_id"] = patient_id
        if arguments.get("meal_date"):
            params["meal_date"] = arguments["meal_date"]
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.get(
                f"{self.system_base_url}/api/agent/nutrition/meals",
                params=params,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name="list_meals",
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:list_meals",
        )

    async def _get_daily_nutrition_summary(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = arguments.get("patient_id") or payload.get("patient_id")
        if patient_id:
            params["patient_id"] = patient_id
        if arguments.get("meal_date"):
            params["meal_date"] = arguments["meal_date"]
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.get(
                f"{self.system_base_url}/api/agent/nutrition/daily-summary",
                params=params,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name="get_daily_nutrition_summary",
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:get_daily_nutrition_summary",
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
            tool_name="record_nutrition_preference",
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:record_nutrition_preference:{fact.get('predicate', 'unknown')}:{fact.get('object_key', 'unknown')}",
        )

    async def _get_nutrition_preferences(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        params = {}
        patient_id = arguments.get("patient_id") or payload.get("patient_id")
        if patient_id:
            params["patient_id"] = patient_id
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.get(
                f"{self.system_base_url}/api/agent/nutrition/preferences",
                params=params,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name="get_nutrition_preferences",
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:get_nutrition_preferences",
        )

    async def _recommend_diet(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            "patient_id": arguments.get("patient_id") or payload.get("patient_id"),
            "constraints": arguments.get("constraints") or {},
            "meal_type": arguments.get("meal_type"),
            "limit": arguments.get("limit", 5),
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.system_base_url}/api/agent/nutrition/recommend",
                json=request_payload,
                headers=self._internal_headers(),
            )
            response.raise_for_status()
        result = response.json()
        return ToolCallResult(
            tool_name="recommend_diet",
            status="success" if result.get("success") else "error",
            response=result,
            error=safe_tool_error(result.get("error")),
            idempotency_key=f"{trace_id}:recommend_diet",
        )

    async def _lookup_side_effect_info(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        request_payload = {
            **arguments,
            "phr_patient_key": arguments.get("phr_patient_key") or payload.get("phr_patient_key"),
            "medication_name": arguments.get("medication_name") or payload.get("medication_name") or context_value(payload, "medication_name"),
            "recent_chat": arguments.get("recent_chat") or payload.get("chat_context") or payload.get("conversation_context") or context_value(payload, "recent_chat") or [],
            "dose_event_id": arguments.get("dose_event_id") or payload.get("dose_event_id") or context_value(payload, "related_dose_event_id"),
        }
        if not request_payload.get("phr_patient_key"):
            return ToolCallResult(
                tool_name="lookup_side_effect_info",
                status="skipped",
                error="phr_registration_required",
                idempotency_key=f"{trace_id}:lookup_side_effect_info",
            )
        request = SideEffectAssessmentRequest.model_validate(request_payload)
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(f"{self.phr_base_url}/phr/side-effects/assess", json=request.model_dump(mode="json"))
            response.raise_for_status()
        result = SideEffectAssessmentResult.model_validate(response.json())
        return ToolCallResult(
            tool_name="lookup_side_effect_info",
            status="success",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:lookup_side_effect_info",
        )

    @staticmethod
    def _ae_pro_ctcae(arguments: dict[str, Any], *, trace_id: str) -> ToolCallResult:
        request = AEProCtcaeAssessmentRequest.model_validate(arguments)
        result = match_pro_ctcae_symptom(request.symptom_normalize or request.symptom_text, threshold=request.threshold)
        return ToolCallResult(
            tool_name="AE_pro_ctcae",
            status="success",
            response=result.model_dump(mode="json"),
            idempotency_key=f"{trace_id}:AE_pro_ctcae",
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
