from __future__ import annotations

import unicodedata
from datetime import date, datetime
from time import perf_counter
from typing import Any

import anyio
import httpx

from agent_app import trace_logging
from agent_app.ae_pro_ctcae import (
    ProCtcaeReferenceUnavailable,
    match_pro_ctcae_symptom,
    match_pro_ctcae_symptom_semantic,
    pro_ctcae_result_for_concept,
    pro_ctcae_source_version,
)
from agent_app.embeddings.base import EmbeddingProvider
from agent_app.embeddings.factory import get_embedding_provider
from agent_app.embeddings.semantic_verifier import (
    LlmSemanticMatchVerifier,
    SemanticMatchVerifier,
)
from agent_app.integration.backend_client import (
    BackendV13Client,
    BackendV13ResponseError,
    BackendV13TransportError,
)
from agent_app.llm.context import context_value
from agent_app.persistence.adverse_reaction_repository import (
    AdverseReactionLookup,
    AgentAdverseReactionRepository,
)
from agent_app.persistence.symptom_concept_repository import (
    ClinicalSymptomConceptMatch,
    SymptomConceptRepository,
)
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.approval_display import (
    approval_display,
    project_record_arguments_to_action_schema,
)
from agent_app.tools.backend_query import BackendQueryTools
from agent_app.tools.backend_write import (
    MODEL_WRITE_ARGUMENTS,
    BackendSyncWriteTools,
    BackendWriteInvocationContext,
    is_backend_v13_sync_write,
)
from agent_app.tools.medication_dose_resolution import (
    resolve_dose_target,
)
from agent_app.tools.medication_side_effects import (
    assess_side_effect_from_snapshot,
    assess_side_effect_from_snapshot_semantic,
    side_effect_record_draft_from_snapshot,
)
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
from shared.backend_v13_contracts import ProCtcaeSeverityResult
from shared.redaction import safe_exception_summary
from shared.schemas import (
    AEProCtcaeAssessmentRequest,
    MedicationDoseStatusResult,
    SideEffectHistoryResult,
    ToolCallResult,
)
from shared.settings import get_settings
from shared.tool_argument_validation import tool_argument_validation_error
from shared.tool_catalog import ToolCatalog
from shared.tool_confirmations import ConfirmationActionRegistry
from shared.tool_names import (
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    CREATE_NUTRITION_MEAL_RECORD,
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
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from shared.tool_permissions import allowed_tool_names_for_source, permission_denied_result, validate_tool_permission

JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601


def _validated_symptom_mentions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        return []
    mentions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw).difference(
            {"text", "onset_text"}
        ):
            return []
        text_value = str(raw.get("text") or "").strip()
        onset_text = str(raw.get("onset_text") or "").strip()
        if not text_value:
            return []
        fingerprint = (text_value.casefold(), onset_text.casefold())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        mentions.append({"text": text_value, "onset_text": onset_text})
    return mentions


def _symptom_concept_payload(
    concept: ClinicalSymptomConceptMatch,
) -> dict[str, Any]:
    return {
        "concept_id": str(concept.concept_id),
        "symptom_term": concept.symptom_term,
        "korean_symptom_name": concept.korean_symptom_name,
        "match_type": concept.match_type,
        "similarity": concept.similarity,
    }


def _symptom_clarification_question(
    symptom_text: str,
    candidates: list[dict[str, Any]],
) -> str:
    labels = list(
        dict.fromkeys(
            str(candidate.get("korean_symptom_name") or "").strip()
            for candidate in candidates
            if str(candidate.get("korean_symptom_name") or "").strip()
        )
    )
    if labels:
        return (
            f"'{symptom_text}'은(는) "
            f"{', '.join(labels)} 중 어느 증상에 더 가깝나요?"
        )
    return f"'{symptom_text}' 증상을 조금 더 구체적으로 알려주세요."


def _side_effect_batch_response(
    assessments: list[dict[str, Any]],
) -> dict[str, Any]:
    matched = [
        item
        for item in assessments
        if item.get("match_status") == "MATCHED"
    ]
    suspected_items = [item for item in matched if item.get("suspected") is True]
    matched_effects = list(
        dict.fromkeys(
            str(value).strip()
            for item in suspected_items
            for value in item.get("matched_effects", [])
            if str(value).strip()
        )
    )
    matched_medications = list(
        dict.fromkeys(
            str(value).strip()
            for item in suspected_items
            for value in item.get("matched_items", [])
            if str(value).strip()
        )
    )
    reference_matches = [
        reference
        for item in suspected_items
        for reference in item.get("reference_matches", [])
        if isinstance(reference, dict)
    ]
    drafts = [
        item["side_effect_record_draft"]
        for item in suspected_items
        if isinstance(item.get("side_effect_record_draft"), dict)
    ]
    response: dict[str, Any] = {
        "suspected": bool(suspected_items),
        "requires_clarification": any(
            item.get("match_status") == "AMBIGUOUS"
            for item in assessments
        ),
        "assessments": assessments,
        "matched_effects": matched_effects,
        "matched_items": matched_medications,
        "severity": (
            str(suspected_items[0].get("severity") or "none")
            if suspected_items
            else "none"
        ),
        "evidence": " ".join(
            str(item.get("evidence") or "").strip()
            for item in suspected_items
            if str(item.get("evidence") or "").strip()
        ),
        "recommendation": " ".join(
            dict.fromkeys(
                str(item.get("recommendation") or "").strip()
                for item in assessments
                if str(item.get("recommendation") or "").strip()
            )
        ),
        "reference_source": (
            str(suspected_items[0].get("reference_source") or "")
            if suspected_items
            else ""
        ),
        "reference_status": (
            str(suspected_items[0].get("reference_status") or "")
            if suspected_items
            else ""
        ),
        "reference_matches": reference_matches,
        "side_effect_record_drafts": drafts,
    }
    if len(drafts) == 1:
        response["side_effect_record_draft"] = drafts[0]
    return response


class AgentMcpToolServer:
    def __init__(
        self,
        *,
        backend_queries: BackendQueryTools | None = None,
        backend_client: BackendV13Client | None = None,
        adverse_reactions: AdverseReactionLookup | None = None,
        llm_provider: BaseLLMProvider | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        semantic_verifier: SemanticMatchVerifier | None = None,
        symptom_concepts: SymptomConceptRepository | None = None,
    ) -> None:
        settings = get_settings()
        self.settings = settings
        from agent_app.persistence.db import engine as agent_engine

        self.agent_engine = agent_engine
        self.backend_queries = backend_queries or BackendQueryTools.from_settings(settings)
        self.backend_client = backend_client or BackendV13Client.from_settings()
        if adverse_reactions is None:
            adverse_reactions = AgentAdverseReactionRepository(agent_engine)
        self.adverse_reactions = adverse_reactions
        self.embedding_provider = (
            embedding_provider or get_embedding_provider()
        )
        self.semantic_verifier = semantic_verifier or (
            LlmSemanticMatchVerifier(llm_provider)
            if llm_provider is not None
            else None
        )
        self.symptom_concepts = (
            symptom_concepts or SymptomConceptRepository(agent_engine)
        )
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
        approved_key_call, rejection = self._tool_call_rejection(
            tool_name,
            arguments,
            trace_id=trace_id,
            source_event_type=source_event_type,
            payload=payload,
        )
        if rejection is not None:
            return rejection
        if tool_name == REQUEST_RECORD_APPROVAL:
            return await self._request_record_approval(
                arguments,
                tool_call_id=tool_call_id,
                trace_id=trace_id,
                source_event_type=source_event_type,
                payload=payload,
            )
        if tool_name in RECORD_APPROVAL_ACTIONS:
            return await self._execute_record_action(
                tool_name,
                arguments,
                approved_key_call=approved_key_call,
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
            return deferred_policy_tool_result(
                {"name": tool_name, "arguments": arguments},
                trace_id=trace_id,
                source_event_type=source_event_type,
            )
        result = await self._execute_general_read_tool(
            tool_name,
            arguments,
            trace_id=trace_id,
            source_event_type=source_event_type,
            payload=payload,
        )
        if result is not None:
            return result
        result = await self._execute_nutrition_read_tool(
            tool_name,
            arguments,
            trace_id=trace_id,
            payload=payload,
        )
        if result is not None:
            return result
        return ToolCallResult(
            tool_name=tool_name or "unknown",
            status="error",
            error=f"unsupported_tool:{tool_name}",
        )

    def _tool_call_rejection(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> tuple[bool, ToolCallResult | None]:
        if tool_name not in ALLOWED_TOOL_NAMES:
            return False, ToolCallResult(
                tool_name=tool_name or "unknown",
                status="error",
                error=f"unsupported_tool:{tool_name}",
            )
        approved_key_call = self._is_approved_key_call(
            tool_name,
            arguments,
            payload,
        )
        denial_reason = None
        if not approved_key_call:
            denial_reason = validate_tool_permission(
                {"name": tool_name, "arguments": arguments},
                source_event_type=source_event_type,
                payload=payload,
            )
        if denial_reason:
            return approved_key_call, permission_denied_result(
                {"name": tool_name, "arguments": arguments},
                trace_id=trace_id,
                source_event_type=source_event_type,
                reason=denial_reason,
            )
        if self._has_unsupported_side_effect_arguments(
            tool_name,
            arguments,
            approved_key_call=approved_key_call,
        ):
            return approved_key_call, ToolCallResult(
                tool_name=tool_name,
                status="error",
                error="unsupported_model_arguments",
                idempotency_key=f"{trace_id}:{tool_name}",
            )
        if (
            not approved_key_call
            and tool_name not in RECORD_APPROVAL_ACTIONS
            and tool_name != REQUEST_RECORD_APPROVAL
        ):
            schema_error = tool_argument_validation_error(
                tool_name,
                arguments,
            )
            if schema_error:
                return approved_key_call, ToolCallResult(
                    tool_name=tool_name,
                    status="error",
                    error="invalid_tool_arguments",
                    response={"reason": schema_error},
                    idempotency_key=f"{trace_id}:{tool_name}",
                )
        return approved_key_call, None

    @staticmethod
    def _has_unsupported_side_effect_arguments(
        tool_name: str,
        arguments: dict[str, Any],
        *,
        approved_key_call: bool,
    ) -> bool:
        if (
            tool_name != CREATE_MEDICATION_SIDE_EFFECT_RECORD
            or approved_key_call
        ):
            return False
        allowed_arguments = {
            "symptom_text",
            "symptom_onset_text",
            "medication_name",
        }
        return bool(set(arguments).difference(allowed_arguments))

    async def _execute_record_action(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        approved_key_call: bool,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> ToolCallResult:
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
        return ToolCallResult(
            tool_name=tool_name,
            status="error",
            error="backend_write_tool_not_supported",
            response={"contract_version": "v1.3"},
            idempotency_key=f"{trace_id}:{tool_name}:unsupported_write",
        )

    async def _execute_general_read_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> ToolCallResult | None:
        if tool_name == GET_MEDICATION_DOSE_STATUS:
            return await self._get_medication_dose_status(
                arguments,
                trace_id=trace_id,
                payload=payload,
            )
        if tool_name == GET_NOTIFICATION_POLICIES:
            return await self._get_notification_policies(
                arguments,
                trace_id=trace_id,
                payload=payload,
            )
        if tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT:
            return await self._get_medication_side_effect_assessment(
                arguments,
                trace_id=trace_id,
                source_event_type=source_event_type,
                payload=payload,
            )
        if tool_name == GET_SIDE_EFFECT_HISTORY:
            return await self._get_side_effect_history(
                arguments,
                trace_id=trace_id,
                payload=payload,
            )
        if tool_name == GET_PRO_CTCAE_QUESTIONNAIRE:
            return await self._ae_pro_ctcae_semantic(
                arguments,
                trace_id=trace_id,
            )
        return None

    async def _execute_nutrition_read_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        trace_id: str,
        payload: dict[str, Any],
    ) -> ToolCallResult | None:
        if tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES:
            return await self._search_nutrition_food_candidates(
                arguments,
                trace_id=trace_id,
                payload=payload,
            )
        if tool_name == GET_NUTRITION_MEAL_RECORD_LIST:
            return await self._get_nutrition_meal_record_list(
                arguments,
                trace_id=trace_id,
                payload=payload,
            )
        if tool_name == GET_NUTRITION_DAILY_SUMMARY:
            return await self._get_nutrition_daily_summary(
                arguments,
                trace_id=trace_id,
                payload=payload,
            )
        if tool_name == GET_NUTRITION_PREFERENCE_SUMMARY:
            return await self._get_nutrition_preference_summary(
                arguments,
                trace_id=trace_id,
                payload=payload,
            )
        if tool_name == GET_NUTRITION_RECOMMENDATION_CANDIDATES:
            return await self._get_nutrition_recommendation_candidates(
                arguments,
                trace_id=trace_id,
                payload=payload,
            )
        return None

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
        if action_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
            resolution = resolve_dose_target(
                record_arguments,
                payload=payload,
            )
            resolution_kind = resolution.get("kind")
            trace_logging.log_info(
                "agent_dose_target_resolution",
                trace_id=trace_id,
                source_event_type=source_event_type,
                resolution_kind=str(resolution_kind or ""),
                supplied_id_present=bool(
                    str(
                        record_arguments.get("dose_event_id")
                        or ""
                    ).strip()
                ),
                medication_reference_present=bool(
                    str(
                        record_arguments.get("medication_name")
                        or ""
                    ).strip()
                ),
                candidate_count=len(
                    resolution.get("candidates") or []
                ),
                resolved_id_present=bool(
                    str(
                        resolution.get("dose_event_id") or ""
                    ).strip()
                ),
            )
            if resolution_kind == "selection_required":
                return ToolCallResult(
                    tool_name=REQUEST_RECORD_APPROVAL,
                    status="success",
                    response={
                        "selection_required": True,
                        "approval_created": False,
                        "record_applied": False,
                        "action_name": action_name,
                        "selection_request": resolution.get(
                            "selection_request"
                        ),
                        "dose_selection": {
                            "action_name": action_name,
                            "candidates": resolution.get(
                                "candidates",
                                [],
                            ),
                        },
                        "message": (
                            "복용한 약을 먼저 선택해야 합니다."
                        ),
                    },
                    idempotency_key=(
                        f"{trace_id}:{REQUEST_RECORD_APPROVAL}:"
                        "dose_selection_required"
                    ),
                )
            if resolution_kind == "clarification_required":
                return ToolCallResult(
                    tool_name=REQUEST_RECORD_APPROVAL,
                    status="success",
                    response={
                        "clarification_required": True,
                        "approval_created": False,
                        "record_applied": False,
                        "action_name": action_name,
                        "message": (
                            "현재 복약 일정에서 대상을 확인하지 "
                            "못했습니다. 복용한 약 이름과 시간을 "
                            "다시 물어보세요."
                        ),
                    },
                    idempotency_key=(
                        f"{trace_id}:{REQUEST_RECORD_APPROVAL}:"
                        "dose_clarification_required"
                    ),
                )
            if resolution_kind == "already_taken":
                candidate = resolution.get("candidate")
                return ToolCallResult(
                    tool_name=REQUEST_RECORD_APPROVAL,
                    status="success",
                    response={
                        "approval_created": False,
                        "record_applied": False,
                        "already_recorded": True,
                        "action_name": action_name,
                        "dose_event": (
                            candidate
                            if isinstance(candidate, dict)
                            else {}
                        ),
                        "message": (
                            "해당 복약은 이미 복용 완료로 "
                            "기록되어 있습니다."
                        ),
                    },
                    idempotency_key=(
                        f"{trace_id}:{REQUEST_RECORD_APPROVAL}:"
                        "dose_already_taken"
                    ),
                )
            resolved_id = str(
                resolution.get("dose_event_id") or ""
            ).strip()
            if resolution_kind != "resolved" or not resolved_id:
                return ToolCallResult(
                    tool_name=REQUEST_RECORD_APPROVAL,
                    status="error",
                    error="dose_event_resolution_invalid",
                    idempotency_key=(
                        f"{trace_id}:{REQUEST_RECORD_APPROVAL}:"
                        "dose_resolution_invalid"
                    ),
                )
            # The approval and eventual write only receive the exact identifier
            # copied from the trusted Backend snapshot.  Medication text from
            # the model remains a lookup hint and is never persisted.
            record_arguments = {"dose_event_id": resolved_id}
        resolved_notification_policy: dict[str, Any] | None = None
        if action_name == CHANGE_NOTIFICATION_POLICY:
            policy_resolution = (
                await self._resolve_active_notification_policy_target(
                    record_arguments,
                    trace_id=trace_id,
                    source_event_type=source_event_type,
                    payload=payload,
                )
            )
            if policy_resolution["kind"] != "resolved":
                return ToolCallResult(
                    tool_name=REQUEST_RECORD_APPROVAL,
                    status="success",
                    response={
                        "policy_target_unavailable": True,
                        "approval_created": False,
                        "record_applied": False,
                        "action_name": action_name,
                        "reason_code": policy_resolution[
                            "reason_code"
                        ],
                        "message": (
                            "수정할 수 있는 활성 알림 정책을 "
                            "확인하지 못했습니다. 채팅에서는 새 "
                            "알림 정책을 생성할 수 없습니다."
                        ),
                    },
                    idempotency_key=(
                        f"{trace_id}:{REQUEST_RECORD_APPROVAL}:"
                        "notification_policy_target_unavailable"
                    ),
                )
            record_arguments = {
                **record_arguments,
                "policy_id": policy_resolution["policy_id"],
            }
            resolved_policy = policy_resolution.get("policy")
            if isinstance(resolved_policy, dict):
                resolved_notification_policy = resolved_policy
        record_arguments = project_record_arguments_to_action_schema(
            action_name,
            record_arguments,
        )
        schema_error = tool_argument_validation_error(
            action_name,
            record_arguments,
        )
        if schema_error:
            return ToolCallResult(
                tool_name=REQUEST_RECORD_APPROVAL,
                status="error",
                error="record_approval_arguments_invalid",
                response={"reason": schema_error},
                idempotency_key=f"{trace_id}:{REQUEST_RECORD_APPROVAL}",
            )
        record_input_request = context_value(
            payload,
            "record_input_request",
        )
        if (
            action_name == CREATE_NUTRITION_MEAL_RECORD
            and isinstance(record_input_request, dict)
            and record_input_request.get("action_name")
            == CREATE_NUTRITION_MEAL_RECORD
        ):
            input_request = record_input_request.get(
                "input_request"
            )
            if not isinstance(input_request, dict):
                return ToolCallResult(
                    tool_name=REQUEST_RECORD_APPROVAL,
                    status="error",
                    error="record_input_request_invalid",
                    idempotency_key=(
                        f"{trace_id}:{REQUEST_RECORD_APPROVAL}"
                    ),
                )
            return ToolCallResult(
                tool_name=REQUEST_RECORD_APPROVAL,
                status="success",
                response={
                    "input_required": True,
                    "approval_created": False,
                    "record_applied": False,
                    "action_name": action_name,
                    "missing_fields": list(
                        record_input_request.get(
                            "missing_fields"
                        )
                        or []
                    ),
                    "input_request": input_request,
                    "message": (
                        "음식 기록 전에 실제 섭취량을 "
                        "입력해야 합니다."
                    ),
                },
                idempotency_key=(
                    f"{trace_id}:{REQUEST_RECORD_APPROVAL}:"
                    "input_required"
                ),
            )
        approval_display_context: dict[str, Any] = {
            "requested_at": payload.get("current_time"),
        }
        if resolved_notification_policy is not None:
            approval_display_context["notification_policy"] = (
                resolved_notification_policy
            )
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

    def _authoritative_side_effect_record_arguments(
        self,
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
            adverse_reactions=self.adverse_reactions,
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
                display=approval_display(
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

    async def _resolve_active_notification_policy_target(
        self,
        record_arguments: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        supplied_policy_id = str(
            record_arguments.get("policy_id") or ""
        ).strip()
        policies: list[dict[str, Any]] = []
        if supplied_policy_id and self.backend_queries is not None:
            result = await anyio.to_thread.run_sync(
                lambda: self.backend_queries.notification_policies(
                    patient_id=str(payload.get("patient_id") or ""),
                    policy_id=supplied_policy_id,
                    active_only=True,
                )
            )
            raw_policies = result.get("policies")
            if isinstance(raw_policies, list):
                policies = [
                    policy
                    for policy in raw_policies
                    if isinstance(policy, dict)
                    and policy.get("active") is True
                    and str(policy.get("policy_id") or "").strip()
                    == supplied_policy_id
                ]

        if not supplied_policy_id:
            kind = "policy_id_missing"
            reason_code = "ACTIVE_NOTIFICATION_POLICY_ID_REQUIRED"
        elif self.backend_queries is None:
            kind = "backend_read_unavailable"
            reason_code = "ACTIVE_NOTIFICATION_POLICY_LOOKUP_UNAVAILABLE"
        elif len(policies) == 1:
            kind = "resolved"
            reason_code = "ACTIVE_NOTIFICATION_POLICY_RESOLVED"
        elif not policies:
            kind = "not_found"
            reason_code = "ACTIVE_NOTIFICATION_POLICY_NOT_FOUND"
        else:
            kind = "ambiguous"
            reason_code = "ACTIVE_NOTIFICATION_POLICY_AMBIGUOUS"

        trace_logging.log_info(
            "agent_notification_policy_target_resolution",
            trace_id=trace_id,
            source_event_type=source_event_type,
            resolution_kind=kind,
            supplied_policy_id_present=bool(supplied_policy_id),
            active_policy_count=len(policies),
        )
        response = {
            "kind": kind,
            "reason_code": reason_code,
        }
        if kind == "resolved":
            response["policy_id"] = str(
                policies[0]["policy_id"]
            )
            response["policy"] = dict(policies[0])
        return response

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
        raw_policies = result.get("policies")
        policies = (
            [item for item in raw_policies if isinstance(item, dict)]
            if isinstance(raw_policies, list)
            else []
        )
        active_policy_count = sum(
            1 for item in policies if item.get("active") is True
        )
        if active_policy_count == 0:
            target_status = "not_found"
        elif active_policy_count == 1:
            target_status = "single"
        else:
            target_status = "multiple"
        response = dict(result)
        response["operation_constraints"] = {
            "creation_supported": False,
            "change_requires_active_policy": True,
            "active_target_status": target_status,
        }
        trace_logging.log_info(
            "agent_notification_policy_lookup_completed",
            trace_id=trace_id,
            active_only=bool(params["active_only"]),
            policy_id_filter_present=bool(params.get("policy_id")),
            slot_label_filter_present=bool(params.get("slot_label")),
            policy_count=len(policies),
            active_policy_count=active_policy_count,
            active_target_status=target_status,
        )
        return ToolCallResult(
            tool_name=GET_NOTIFICATION_POLICIES,
            status="success",
            response=response,
            idempotency_key=f"{trace_id}:{GET_NOTIFICATION_POLICIES}",
        )

    async def _search_nutrition_food_candidates(self, arguments: dict[str, Any], *, trace_id: str, payload: dict[str, Any]) -> ToolCallResult:
        raw_queries = arguments.get("food_queries")
        food_queries: list[str] = []
        seen_queries: set[str] = set()
        if isinstance(raw_queries, list):
            for query in raw_queries:
                text = str(query).strip()
                normalized = unicodedata.normalize(
                    "NFKC",
                    text,
                ).casefold()
                if not text or normalized in seen_queries:
                    continue
                seen_queries.add(normalized)
                food_queries.append(text)
        request_payload = {
            "food_queries": food_queries,
            "limit_per_query": arguments.get(
                "limit_per_query",
                6,
            ),
            "patient_id": self._patient_id_for_tool(arguments, payload),
            "meal_type": arguments.get("meal_type"),
        }
        if self.backend_queries is None:
            return backend_read_unavailable_result(
                SEARCH_NUTRITION_FOOD_CANDIDATES,
                trace_id=trace_id,
            )
        search_groups = []
        for query in food_queries:
            result = await anyio.to_thread.run_sync(
                lambda query=query: (
                    self.backend_queries.search_food_candidates(
                        query=query,
                        limit=int(
                            request_payload[
                                "limit_per_query"
                            ]
                        ),
                    )
                )
            )
            search_groups.append(
                {
                    "query": query,
                    "candidates": result.get(
                        "candidates",
                        [],
                    ),
                    "match_mode": result.get(
                        "match_mode",
                        "none",
                    ),
                    "source": result.get(
                        "source",
                        "backend_read_db",
                    ),
                }
            )
        result = {
            "success": bool(search_groups),
            "search_groups": search_groups,
            "food_queries": food_queries,
            "meal_type": str(
                request_payload.get("meal_type") or ""
            ),
            "limit_per_query": int(
                request_payload["limit_per_query"]
            ),
            "total_groups": len(search_groups),
        }
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
        for key in ("meal_date", "start_date", "end_date"):
            if arguments.get(key):
                params[key] = arguments[key]
        if not any(
            params.get(key)
            for key in ("meal_date", "start_date", "end_date")
        ):
            current_date = _payload_current_date(payload)
            if current_date is not None:
                params["meal_date"] = current_date
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
        symptom_mentions = _validated_symptom_mentions(
            arguments.get("symptom_mentions")
        )
        if not symptom_mentions:
            return ToolCallResult(
                tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                status="error",
                error="symptom_mentions_required",
                idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
            )
        medication_name = str(
            arguments.get("medication_name")
            or payload.get("medication_name")
            or context_value(payload, "medication_name")
            or ""
        ).strip()
        try:
            assessments: list[dict[str, Any]] = []
            if isinstance(
                self.adverse_reactions,
                AgentAdverseReactionRepository,
            ):
                if self.semantic_verifier is None:
                    raise RuntimeError(
                        "reference_semantic_verifier_unavailable"
                    )
                pro_source = pro_ctcae_source_version(
                    self.settings.pro_ctcae_workbook_path
                )
                for mention_index, mention in enumerate(symptom_mentions):
                    symptom_text = mention["text"]
                    resolution = (
                        await self.symptom_concepts.resolve_with_status(
                            symptom_text=symptom_text,
                            pro_ctcae_source_version=pro_source,
                            embedding_provider=self.embedding_provider,
                            semantic_verifier=self.semantic_verifier,
                            top_k=self.settings.pro_ctcae_vector_top_k,
                            min_similarity=(
                                self.settings
                                .reference_vector_min_similarity
                            ),
                        )
                    )
                    candidate_payloads = [
                        _symptom_concept_payload(candidate)
                        for candidate in resolution.candidates
                    ]
                    if resolution.status != "MATCHED":
                        assessments.append(
                            {
                                "mention_index": mention_index,
                                "symptom_text": symptom_text,
                                "symptom_onset_text": mention["onset_text"],
                                "match_status": resolution.status,
                                "matched_concept": None,
                                "candidate_concepts": candidate_payloads,
                                "clarification_question": (
                                    _symptom_clarification_question(
                                        symptom_text,
                                        candidate_payloads,
                                    )
                                    if resolution.status == "AMBIGUOUS"
                                    else ""
                                ),
                                "suspected": False,
                                "matched_effects": [],
                                "matched_items": [],
                                "severity": "none",
                                "evidence": "",
                                "recommendation": (
                                    "증상 표현을 더 구체적으로 확인해야 합니다."
                                    if resolution.status == "AMBIGUOUS"
                                    else "매칭되는 표준 증상 개념을 확인하지 못했습니다."
                                ),
                                "reference_source": "",
                                "reference_status": "",
                                "reference_matches": [],
                            }
                        )
                        continue
                    symptom_concept = resolution.match
                    if symptom_concept is None:
                        raise RuntimeError(
                            "matched_symptom_concept_missing"
                        )
                    await anyio.to_thread.run_sync(
                        lambda symptom_text=symptom_text,
                        symptom_concept=symptom_concept: (
                            self.symptom_concepts.remember(
                                trace_id=trace_id,
                                symptom_text=symptom_text,
                                concept=symptom_concept,
                                ttl_seconds=(
                                    self.settings
                                    .agent_symptom_resolution_ttl_seconds
                                ),
                            )
                        )
                    )
                    result = await assess_side_effect_from_snapshot_semantic(
                        symptom_text=symptom_text,
                        patient_snapshot=patient_snapshot,
                        medication_name=medication_name or None,
                        adverse_reactions=self.adverse_reactions,
                        embedding_provider=self.embedding_provider,
                        semantic_verifier=self.semantic_verifier,
                        symptom_concept=symptom_concept,
                        symptom_concepts=self.symptom_concepts,
                        top_k=self.settings.adverse_reaction_vector_top_k,
                        min_similarity=(
                            self.settings.reference_vector_min_similarity
                        ),
                    )
                    assessment = result.model_dump(mode="json")
                    assessment.update(
                        {
                            "mention_index": mention_index,
                            "symptom_text": symptom_text,
                            "symptom_onset_text": mention["onset_text"],
                            "match_status": "MATCHED",
                            "matched_concept": _symptom_concept_payload(
                                symptom_concept
                            ),
                            "candidate_concepts": candidate_payloads,
                            "clarification_question": "",
                        }
                    )
                    assessment["side_effect_record_draft"] = (
                        side_effect_record_draft_from_snapshot(
                            symptom_text=symptom_text,
                            symptom_onset_text=mention["onset_text"],
                            medication_name=medication_name or None,
                            patient_snapshot=patient_snapshot,
                            trace_id=trace_id,
                            source_event_type=source_event_type,
                            assessment=result,
                        )
                    )
                    assessments.append(assessment)
            else:
                for mention_index, mention in enumerate(symptom_mentions):
                    symptom_text = mention["text"]
                    result = assess_side_effect_from_snapshot(
                        symptom_text=symptom_text,
                        patient_snapshot=patient_snapshot,
                        medication_name=medication_name or None,
                        adverse_reactions=self.adverse_reactions,
                    )
                    assessment = result.model_dump(mode="json")
                    assessment.update(
                        {
                            "mention_index": mention_index,
                            "symptom_text": symptom_text,
                            "symptom_onset_text": mention["onset_text"],
                            "match_status": "MATCHED",
                            "matched_concept": None,
                            "candidate_concepts": [],
                            "clarification_question": "",
                        }
                    )
                    assessment["side_effect_record_draft"] = (
                        side_effect_record_draft_from_snapshot(
                            symptom_text=symptom_text,
                            symptom_onset_text=mention["onset_text"],
                            medication_name=medication_name or None,
                            patient_snapshot=patient_snapshot,
                            trace_id=trace_id,
                            source_event_type=source_event_type,
                            assessment=result,
                        )
                    )
                    assessments.append(assessment)
        except ValueError as exc:
            return ToolCallResult(
                tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                status="error",
                error=str(exc),
                idempotency_key=f"{trace_id}:{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}",
            )
        response_payload = _side_effect_batch_response(assessments)
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

    async def _ae_pro_ctcae_semantic(
        self,
        arguments: dict[str, Any],
        *,
        trace_id: str,
    ) -> ToolCallResult:
        request = AEProCtcaeAssessmentRequest.model_validate(arguments)
        if self.semantic_verifier is None:
            return ToolCallResult(
                tool_name=GET_PRO_CTCAE_QUESTIONNAIRE,
                status="error",
                error="reference_semantic_verifier_unavailable",
                idempotency_key=(
                    f"{trace_id}:{GET_PRO_CTCAE_QUESTIONNAIRE}"
                ),
            )
        try:
            pro_source = pro_ctcae_source_version(
                self.settings.pro_ctcae_workbook_path
            )
            concept = await anyio.to_thread.run_sync(
                lambda: self.symptom_concepts.recall(
                    trace_id=trace_id,
                    symptom_text=request.symptom_text,
                    pro_ctcae_source_version=pro_source,
                )
            )
            if concept is not None:
                result = pro_ctcae_result_for_concept(
                    request.symptom_text,
                    concept,
                    workbook_path=(
                        self.settings.pro_ctcae_workbook_path
                    ),
                )
            else:
                result = await match_pro_ctcae_symptom_semantic(
                    request.symptom_text,
                    engine=self.agent_engine,
                    embedding_provider=self.embedding_provider,
                    semantic_verifier=self.semantic_verifier,
                    top_k=self.settings.pro_ctcae_vector_top_k,
                    min_similarity=(
                        self.settings.reference_vector_min_similarity
                    ),
                )
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
            idempotency_key=(
                f"{trace_id}:{GET_PRO_CTCAE_QUESTIONNAIRE}"
            ),
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
