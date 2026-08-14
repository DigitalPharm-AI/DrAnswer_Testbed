from __future__ import annotations

from time import perf_counter
from typing import Any

from agent_app import trace_logging
from agent_app.agents.medication import MedicationAgent
from agent_app.agents.multiturn_approved_write import (
    execute_approved_supervisor_write,
)
from agent_app.agents.multiturn_graph import build_multiturn_graph
from agent_app.agents.multiturn_output import (
    MULTITURN_CHAT_AGENT_NAME,
    normalize_mutation_confirmation_output,
    validate_multiturn_output,
)
from agent_app.agents.multiturn_responses import mutation_confirmation_from_state
from agent_app.agents.multiturn_state import MultiturnGraphState
from agent_app.agents.multiturn_tools import (
    append_blocked_supervisor_calls,
    delegation_request_payload,
    delegation_tool_result,
)
from agent_app.agents.nutrition_management import NutritionManagementAgent
from agent_app.agents.nutrition_recommendation import NutritionRecommendationAgent
from agent_app.agents.tool_chat import (
    ITERATIVE_TOOL_EXECUTION_MODE,
    TOOL_LOOP_MODE,
)
from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import PROMPT_VERSION_ID, agent_error
from agent_app.llm.messages import (
    ai_message_from_tool_calls,
    build_chat_messages,
    langchain_tools_from_catalog,
    model_output_from_ai_message,
    model_output_with_tool_calls,
    public_text_from_ai_message,
    tool_calls_from_ai_message,
    tool_messages_from_results,
)
from agent_app.llm.prompts import (
    multiturn_chat_prompt,
    mutation_confirmation_prompt,
    mutation_confirmation_reply_prompt,
    mutation_resolution_prompt,
)
from agent_app.llm.side_effect_guard import (
    SIDE_EFFECT_FEATURE_UNAVAILABLE_MESSAGE,
    is_medication_side_effect_request,
)
from agent_app.llm.validation import validate_mutation_confirmation_reply_output
from agent_app.observability.model_calls import (
    traced_model_ainvoke,
    traced_model_astream_message,
)
from agent_app.orchestration.continuation import continuation_type
from agent_app.orchestration.delegation import (
    delegation_target,
    delegation_tools_payload,
    is_delegation_tool_call,
)
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.budget import (
    current_tool_execution_budget,
    response_with_tool_execution_budget,
    tool_execution_budget_scope,
)
from agent_app.tools.policy import normalize_policy_tool_calls
from agent_app.tools.policy_gate import (
    ToolCallContext,
    ToolCallOrigin,
    tool_call_fingerprint,
)
from agent_app.tools.runtime import ToolRuntime
from agent_app.tools.side_effects import is_medication_side_effect_feature_call
from shared.schemas import AgentResponse, MultiturnChatRequest
from shared.tool_catalog import ToolCatalog
from shared.tool_names import (
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    DELEGATE_TO_MEDICATION_AGENT,
    DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
    GET_NOTIFICATION_POLICIES,
    PROPOSE_SYSTEM_POLICY,
    RECORD_APPROVAL_ACTIONS,
    REQUEST_RECORD_APPROVAL,
    SIDE_EFFECT_TOOLS,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)

SUPERVISOR_DIRECT_TOOLS = tuple(
    sorted(
        {
            PROPOSE_SYSTEM_POLICY,
            GET_NOTIFICATION_POLICIES,
            REQUEST_RECORD_APPROVAL,
            CHANGE_NOTIFICATION_POLICY,
        }
    )
)


class MultiturnChatAgent:
    def __init__(
        self,
        provider: BaseLLMProvider,
        tool_runtime: ToolRuntime,
        *,
        nutrition_recommendation_enabled: bool = True,
        medication_side_effect_enabled: bool = True,
    ) -> None:
        self.nutrition_recommendation_enabled = nutrition_recommendation_enabled
        self.medication_side_effect_enabled = medication_side_effect_enabled
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.medication_agent = MedicationAgent(
            provider,
            tool_runtime,
            medication_side_effect_enabled=medication_side_effect_enabled,
        )
        self.nutrition_management_agent = NutritionManagementAgent(provider, tool_runtime)
        self.nutrition_recommendation_agent = NutritionRecommendationAgent(provider, tool_runtime)
        self.graph = build_multiturn_graph(self)

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        with tool_execution_budget_scope() as budget:
            response = await self._run_without_budget(trace_id, payload)
            return response_with_tool_execution_budget(response, budget)

    async def _run_without_budget(
        self,
        trace_id: str,
        payload: dict[str, Any],
    ) -> AgentResponse:
        try:
            request = MultiturnChatRequest.model_validate(payload)
            request_payload = request.model_dump(mode="json")
            context = request.context if isinstance(request.context, dict) else {}
            approved_action = context.get("approved_user_action")
            approved_action_name = str(approved_action.get("action_name") or "") if isinstance(approved_action, dict) else ""
            if isinstance(approved_action, dict) and approved_action.get("status") == "confirmed" and approved_action_name in RECORD_APPROVAL_ACTIONS:
                model_arguments = approved_action.get("arguments")
                if not isinstance(model_arguments, dict):
                    raise ValueError("approved_user_action_arguments_missing")
                approved_tool_call = {
                    "id": f"approved_{approved_action_name}",
                    "name": approved_action_name,
                    "arguments": model_arguments,
                }
                if approved_action_name == CHANGE_NOTIFICATION_POLICY:
                    return await execute_approved_supervisor_write(
                        self.provider,
                        self.tool_runtime,
                        trace_id,
                        request_payload,
                        approved_tool_call,
                    )
                else:
                    target_agent = (
                        self.medication_agent
                        if approved_action_name
                        in {
                            CREATE_MEDICATION_SIDE_EFFECT_RECORD,
                            UPDATE_MEDICATION_DOSE_EVENT_STATUS,
                        }
                        else self.nutrition_management_agent
                    )
                    specialist_response = await target_agent.execute_approved_write(
                        trace_id,
                        request_payload,
                        tool_call=approved_tool_call,
                    )
                    return specialist_response
            final_state = await self.graph.ainvoke(
                {
                    "trace_id": trace_id,
                    "request_payload": request_payload,
                    "context": context,
                    "specialist_continuation_tool_calls": [],
                    "seeded_tool_calls_consumed": False,
                    "seeded_tool_origin": ToolCallOrigin.MODEL,
                    "pending_tool_call_origin": ToolCallOrigin.MODEL,
                    "all_supervisor_tool_calls": [],
                    "all_supervisor_tool_results": [],
                    "all_tool_messages": [],
                    "delegation_calls": [],
                    "delegated_responses": [],
                    "direct_tool_calls": [],
                    "direct_tool_results": [],
                    "tool_round_result_counts": [],
                    "iterations": 0,
                    "side_effect_guard_checked": self.medication_side_effect_enabled,
                }
            )
            response = final_state["response"]
            reply_intent = str(final_state.get("confirmation_reply_intent") or context.get("pending_mutation_confirmation_reply_resolved") or "")
            if reply_intent:
                structured_payload = dict(response.structured_payload)
                structured_payload["mutation_confirmation_reply"] = {"intent": reply_intent}
                revision = final_state.get("context", {}).get("mutation_confirmation_revision")
                if reply_intent == "revise" and isinstance(revision, dict):
                    structured_payload["mutation_confirmation_revision"] = revision
                response = response.model_copy(update={"structured_payload": structured_payload})
            return response
        except Exception as exc:
            raise agent_error(trace_id, MULTITURN_CHAT_AGENT_NAME, "system_guidance", exc) from exc

    @staticmethod
    def _prepare_confirmation_reply_model(state: MultiturnGraphState) -> dict[str, Any]:
        pending_confirmation = state.get("context", {}).get("pending_mutation_confirmation")
        if not isinstance(pending_confirmation, dict):
            raise ValueError("pending_mutation_confirmation_required")
        messages = build_chat_messages(
            mutation_confirmation_reply_prompt(),
            {
                "response_mode": "mutation_confirmation_reply",
                "user_reply": state["request_payload"].get("message"),
                "pending_change": {
                    "display": pending_confirmation.get("display", {}),
                    "original_request": pending_confirmation.get("original_request", ""),
                },
            },
        )
        return {"messages": messages}

    async def _confirmation_reply_llm(self, state: MultiturnGraphState) -> dict[str, Any]:
        started = perf_counter()
        ai_message = await traced_model_ainvoke(
            self.provider.chat_model(),
            state["messages"],
            name="multiturn_chat.confirmation_reply",
            prompt_version_id=PROMPT_VERSION_ID,
        )
        elapsed_ms = round((perf_counter() - started) * 1000)
        native_tool_calls = tool_calls_from_ai_message(ai_message)
        output = model_output_from_ai_message(ai_message)
        serialized_tool_calls = output.get("tool_calls")
        if native_tool_calls or isinstance(output.get("tool_call"), dict) or (isinstance(serialized_tool_calls, list) and serialized_tool_calls):
            raise AgentExecutionError(
                "mutation_confirmation_reply_returned_tool_calls",
                error_type="llm_output_validation_failed",
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                decision_type="mutation_confirmation_reply",
            )
        validated = validate_mutation_confirmation_reply_output(output)
        trace_logging.log_info(
            "agent_llm_node_completed",
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            node="mutation_confirmation_reply_llm",
            elapsed_ms=elapsed_ms,
            tools_bound=False,
            intent=validated["intent"],
        )
        updates: dict[str, Any] = {
            "confirmation_reply_ai_message": ai_message,
            "confirmation_reply_model_output": validated,
            "confirmation_reply_intent": validated["intent"],
            "confirmation_reply_llm_elapsed_ms": elapsed_ms,
        }
        if validated["intent"] in {"new_request", "revise"}:
            context = dict(state.get("context", {}))
            pending_confirmation = context.get("pending_mutation_confirmation")
            context.pop("pending_mutation_confirmation", None)
            request_metadata = dict(context.get("request_metadata") or {})
            request_metadata.pop("pending_mutation_confirmation", None)
            context["request_metadata"] = request_metadata
            context["pending_mutation_confirmation_reply_resolved"] = validated["intent"]
            if validated["intent"] == "revise" and isinstance(pending_confirmation, dict):
                context["mutation_confirmation_revision"] = {
                    "display": pending_confirmation.get("display", {}),
                    "original_request": pending_confirmation.get("original_request", ""),
                    "user_revision": state["request_payload"].get("message", ""),
                }
            request_payload = dict(state["request_payload"])
            request_payload["context"] = context
            updates["context"] = context
            updates["request_payload"] = request_payload
        return updates

    def _prepare_mutation_resolution_model(self, state: MultiturnGraphState) -> dict[str, Any]:
        resolution = state.get("context", {}).get("mutation_resolution")
        request_payload = state["request_payload"]
        recommendation_enabled = self.nutrition_recommendation_enabled
        catalog_tools = [
            *ToolCatalog.model_tools_for(*SUPERVISOR_DIRECT_TOOLS),
            *delegation_tools_payload(
                nutrition_recommendation_enabled=recommendation_enabled,
                medication_side_effect_enabled=self.medication_side_effect_enabled,
            ),
        ]
        messages = build_chat_messages(
            mutation_resolution_prompt(
                nutrition_recommendation_enabled=recommendation_enabled,
                medication_side_effect_enabled=self.medication_side_effect_enabled,
            ),
            {
                **request_payload,
                "response_mode": "mutation_resolution_continuation",
                "original_request": request_payload.get("message"),
                "context": {**state.get("context", {}), "mutation_resolution": resolution},
                "available_tools": catalog_tools,
            },
        )
        bound_model = self.provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
        return {"messages": messages, "bound_model": bound_model}

    def _prepare_model(self, state: MultiturnGraphState) -> dict[str, Any]:
        request_payload = state["request_payload"]
        recommendation_enabled = self.nutrition_recommendation_enabled
        catalog_tools = [
            *ToolCatalog.model_tools_for(*SUPERVISOR_DIRECT_TOOLS),
            *delegation_tools_payload(
                nutrition_recommendation_enabled=recommendation_enabled,
                medication_side_effect_enabled=self.medication_side_effect_enabled,
            ),
        ]
        messages = build_chat_messages(
            multiturn_chat_prompt(
                nutrition_recommendation_enabled=recommendation_enabled,
                medication_side_effect_enabled=self.medication_side_effect_enabled,
            ),
            {
                **request_payload,
                "response_mode": "multiturn_chat",
                "decision_type": "system_guidance",
                "available_tools": catalog_tools,
            },
        )
        bound_model = self.provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
        return {"messages": messages, "bound_model": bound_model}

    async def _supervisor_llm(self, state: MultiturnGraphState) -> dict[str, Any]:
        request_payload = state["request_payload"]
        context = state.get("context", {})
        continuation_tool_calls = context.get("continuation_tool_calls")
        seeded_tool_calls_consumed = state.get(
            "seeded_tool_calls_consumed",
            False,
        )
        specialist_continuation_tool_calls = state.get(
            "specialist_continuation_tool_calls",
            [],
        )
        seeded_tool_origin = state.get(
            "seeded_tool_origin",
            ToolCallOrigin.MODEL,
        )
        resolution_elapsed_ms: int | None = None

        if context.get("execute_tool_continuation") is True and isinstance(continuation_tool_calls, list) and not seeded_tool_calls_consumed:
            try:
                seeded_tool_origin = ToolCallOrigin(str(context.get("continuation_tool_origin") or ToolCallOrigin.CLINICAL_CONTINUATION.value))
            except ValueError:
                seeded_tool_origin = ToolCallOrigin.CLINICAL_CONTINUATION
            tool_calls = normalize_policy_tool_calls(
                continuation_tool_calls,
                source_event_type="multiturn_chat",
            )
            if any(str(call.get("name") or "") in SIDE_EFFECT_TOOLS for call in tool_calls):
                specialist_continuation_tool_calls = tool_calls
                tool_calls = [
                    {
                        "name": DELEGATE_TO_MEDICATION_AGENT,
                        "arguments": {
                            "task": "Continue the deferred medication side-effect assessment.",
                            "reason": "medication_tool_continuation",
                        },
                    }
                ]
            output = model_output_with_tool_calls({}, tool_calls)
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content="",
                model_output=output,
            )
            final_text = ""
            seeded_tool_calls_consumed = True
            pending_tool_call_origin = seeded_tool_origin
        else:
            started = perf_counter()
            ai_message = await traced_model_astream_message(
                state["bound_model"],
                state["messages"],
                name="multiturn_chat.supervisor",
                prompt_version_id=PROMPT_VERSION_ID,
                publish_public_text=self.medication_side_effect_enabled,
            )
            if state.get("entry_mode") == "mutation_resolution":
                resolution_elapsed_ms = round((perf_counter() - started) * 1000)
                trace_logging.log_info(
                    "agent_llm_node_completed",
                    trace_id=state["trace_id"],
                    agent_name=MULTITURN_CHAT_AGENT_NAME,
                    node="mutation_resolution_llm",
                    elapsed_ms=resolution_elapsed_ms,
                    tools_bound=True,
                )
            tool_calls = normalize_policy_tool_calls(
                tool_calls_from_ai_message(ai_message),
                source_event_type="multiturn_chat",
            )
            if tool_calls:
                output = model_output_with_tool_calls({}, tool_calls)
                final_text = ""
            else:
                output = {}
                final_text = public_text_from_ai_message(ai_message)
                if not final_text:
                    raise AgentExecutionError(
                        "llm_final_answer_missing",
                        error_type="llm_final_answer_missing",
                        trace_id=state["trace_id"],
                        agent_name=MULTITURN_CHAT_AGENT_NAME,
                        decision_type="system_guidance",
                    )
            pending_tool_call_origin = ToolCallOrigin.MODEL
            if seeded_tool_origin is ToolCallOrigin.APPROVED_WRITE and seeded_tool_calls_consumed and tool_calls:
                raise AgentExecutionError(
                    "approved_write_followup_tool_forbidden",
                    error_type="approved_write_followup_tool_forbidden",
                    trace_id=state["trace_id"],
                    agent_name=MULTITURN_CHAT_AGENT_NAME,
                    decision_type="system_guidance",
                )

        self._validate_enabled_delegation_calls(state["trace_id"], tool_calls)
        self._validate_enabled_side_effect_calls(state["trace_id"], tool_calls)

        current_tool_execution_budget().validate_turn_plan(
            tool_calls,
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type="system_guidance",
        )
        validation_output = model_output_with_tool_calls(output, tool_calls)
        if tool_calls:
            validate_multiturn_output(
                state["trace_id"],
                validation_output,
                request_payload,
            )
        if tool_calls:
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=ai_message.content,
                model_output=validation_output,
            )

        updates: dict[str, Any] = {
            "current_ai_message": ai_message,
            "current_model_output": validation_output,
            "current_final_text": final_text,
            "pending_tool_calls": tool_calls,
            "continuation_type": continuation_type(tool_calls),
            "specialist_continuation_tool_calls": (specialist_continuation_tool_calls),
            "seeded_tool_calls_consumed": seeded_tool_calls_consumed,
            "seeded_tool_origin": seeded_tool_origin,
            "pending_tool_call_origin": pending_tool_call_origin,
            "tool_dispatch_index": 0,
            "round_executed_calls": [],
            "round_tool_results": [],
            "round_delegation_calls": [],
            "round_delegated_responses": [],
            "round_direct_tool_calls": [],
            "round_direct_tool_results": [],
            "round_confirmation_required": False,
            "confirmation_required": False,
            "side_effect_guard_checked": (
                self.medication_side_effect_enabled or bool(tool_calls)
            ),
        }
        if "initial_model_output" not in state:
            updates["initial_model_output"] = dict(validation_output)
        if resolution_elapsed_ms is not None:
            updates["mutation_resolution_llm_timings_ms"] = [
                *state.get("mutation_resolution_llm_timings_ms", []),
                resolution_elapsed_ms,
            ]
        return updates

    async def _side_effect_response_guard(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]:
        if self.medication_side_effect_enabled:
            return {"side_effect_guard_checked": True}
        candidate_response = state.get("current_final_text", "").strip()
        side_effect_request = await is_medication_side_effect_request(
            self.provider,
            user_message=str(state["request_payload"].get("message") or ""),
            candidate_response=candidate_response,
        )
        return {
            "current_final_text": (
                SIDE_EFFECT_FEATURE_UNAVAILABLE_MESSAGE
                if side_effect_request
                else candidate_response
            ),
            "side_effect_guard_checked": True,
        }

    @staticmethod
    def _pending_dispatch_call(
        state: MultiturnGraphState,
    ) -> tuple[int, dict[str, Any]]:
        index = state.get("tool_dispatch_index", 0)
        pending_calls = state.get("pending_tool_calls", [])
        if index >= len(pending_calls):
            raise RuntimeError("supervisor_tool_dispatch_index_out_of_range")
        return index, pending_calls[index]

    async def _supervisor_direct_tool_node(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]:
        index, call = self._pending_dispatch_call(state)
        if is_delegation_tool_call(call):
            raise RuntimeError("delegation_routed_to_direct_tool_node")
        runtime_calls, runtime_results = await self.tool_runtime.execute(
            [call],
            trace_id=state["trace_id"],
            source_event_type="multiturn_chat",
            payload=state["request_payload"],
            call_context=ToolCallContext(
                origin=state.get(
                    "pending_tool_call_origin",
                    ToolCallOrigin.MODEL,
                ),
                prior_call_fingerprints=frozenset(
                    tool_call_fingerprint(previous_call)
                    for previous_call in [
                        *state.get("all_supervisor_tool_calls", []),
                        *state.get("round_executed_calls", []),
                    ]
                ),
            ),
            routing_context={
                "routing_mode": "supervisor_tool_loop",
                "executed_by": MULTITURN_CHAT_AGENT_NAME,
                "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                "agent_graph_mode": TOOL_LOOP_MODE,
                "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                "tool_loop_mode": TOOL_LOOP_MODE,
                "supervisor_tool_names": [str(call.get("name") or "")],
                "tool_names": [str(call.get("name") or "")],
            },
        )
        if len(runtime_calls) != len(runtime_results):
            raise RuntimeError("supervisor_tool_execution_result_count_mismatch")
        return {
            "tool_dispatch_index": index + 1,
            "round_executed_calls": [
                *state.get("round_executed_calls", []),
                *runtime_calls,
            ],
            "round_tool_results": [
                *state.get("round_tool_results", []),
                *runtime_results,
            ],
            "round_direct_tool_calls": [
                *state.get("round_direct_tool_calls", []),
                *runtime_calls,
            ],
            "round_direct_tool_results": [
                *state.get("round_direct_tool_results", []),
                *runtime_results,
            ],
            "round_confirmation_required": any(result.status == "confirmation_required" for result in runtime_results),
        }

    async def _medication_specialist_subgraph_node(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]:
        return await self._delegated_specialist_subgraph_node(
            state,
            expected_target="medication_agent",
        )

    async def _nutrition_management_specialist_subgraph_node(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]:
        return await self._delegated_specialist_subgraph_node(
            state,
            expected_target="nutrition_management_agent",
        )

    async def _nutrition_recommendation_specialist_subgraph_node(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]:
        return await self._delegated_specialist_subgraph_node(
            state,
            expected_target="nutrition_recommendation_agent",
        )

    async def _delegated_specialist_subgraph_node(
        self,
        state: MultiturnGraphState,
        *,
        expected_target: str,
    ) -> dict[str, Any]:
        index, call = self._pending_dispatch_call(state)
        actual_target = delegation_target(call)
        if actual_target != expected_target:
            raise RuntimeError(f"specialist_subgraph_route_mismatch:{expected_target}:{actual_target or 'unknown'}")
        current_tool_execution_budget().consume_tool_call(
            str(call.get("name") or "delegated_agent"),
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type="delegation",
            delegation=True,
        )
        trace_logging.log_info(
            "agent_specialist_subgraph_started",
            trace_id=state["trace_id"],
            supervisor_agent=MULTITURN_CHAT_AGENT_NAME,
            specialist_agent=expected_target,
            graph_node=f"{expected_target}_subgraph",
            tool_name=str(call.get("name") or "delegated_agent"),
            tool_dispatch_index=index,
        )
        delegation_payload = delegation_request_payload(state, call)
        delegated_response = await self._run_delegation(
            state["trace_id"],
            delegation_payload,
            call,
            continuation_tool_calls=(state.get("specialist_continuation_tool_calls", []) or None),
        )
        result = delegation_tool_result(
            state["trace_id"],
            call,
            delegated_response,
            sequence=(len(state.get("delegation_calls", [])) + len(state.get("round_delegation_calls", []))),
        )
        trace_logging.log_info(
            "agent_specialist_subgraph_completed",
            trace_id=state["trace_id"],
            supervisor_agent=MULTITURN_CHAT_AGENT_NAME,
            specialist_agent=expected_target,
            graph_node=f"{expected_target}_subgraph",
            decision_type=delegated_response.decision_type,
            result_status=result.status,
            tool_dispatch_index=index,
        )
        return {
            "tool_dispatch_index": index + 1,
            "round_executed_calls": [
                *state.get("round_executed_calls", []),
                call,
            ],
            "round_tool_results": [
                *state.get("round_tool_results", []),
                result,
            ],
            "round_delegation_calls": [
                *state.get("round_delegation_calls", []),
                call,
            ],
            "round_delegated_responses": [
                *state.get("round_delegated_responses", []),
                delegated_response,
            ],
            "round_confirmation_required": (result.status == "confirmation_required"),
            "specialist_continuation_tool_calls": [],
        }

    def _complete_supervisor_tool_round(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]:
        executed_calls = list(state.get("round_executed_calls", []))
        results = list(state.get("round_tool_results", []))
        confirmation_result = next(
            (result for result in results if result.status == "confirmation_required"),
            None,
        )
        if confirmation_result is not None:
            append_blocked_supervisor_calls(
                state.get("pending_tool_calls", [])[state.get("tool_dispatch_index", 0) :],
                executed_calls,
                results,
                trace_id=state["trace_id"],
                confirmation_tool=confirmation_result.tool_name,
            )
        if len(executed_calls) != len(results):
            raise RuntimeError("supervisor_tool_execution_result_count_mismatch")

        ai_message = state["current_ai_message"]
        output = state.get("current_model_output", {})
        executed_ai_message = ai_message_from_tool_calls(
            executed_calls,
            content=ai_message.content,
            model_output=output,
        )
        tool_messages = tool_messages_from_results(
            executed_ai_message,
            executed_calls,
            results,
        )
        return {
            "messages": [*state["messages"], executed_ai_message, *tool_messages],
            "all_supervisor_tool_calls": [
                *state.get("all_supervisor_tool_calls", []),
                *executed_calls,
            ],
            "all_supervisor_tool_results": [
                *state.get("all_supervisor_tool_results", []),
                *results,
            ],
            "all_tool_messages": [
                *state.get("all_tool_messages", []),
                *tool_messages,
            ],
            "delegation_calls": [
                *state.get("delegation_calls", []),
                *state.get("round_delegation_calls", []),
            ],
            "delegated_responses": [
                *state.get("delegated_responses", []),
                *state.get("round_delegated_responses", []),
            ],
            "direct_tool_calls": [
                *state.get("direct_tool_calls", []),
                *state.get("round_direct_tool_calls", []),
            ],
            "direct_tool_results": [
                *state.get("direct_tool_results", []),
                *state.get("round_direct_tool_results", []),
            ],
            "tool_round_result_counts": [
                *state.get("tool_round_result_counts", []),
                len(results),
            ],
            "iterations": state.get("iterations", 0) + 1,
            "pending_tool_calls": [],
            "tool_dispatch_index": 0,
            "round_executed_calls": [],
            "round_tool_results": [],
            "round_delegation_calls": [],
            "round_delegated_responses": [],
            "round_direct_tool_calls": [],
            "round_direct_tool_results": [],
            "round_confirmation_required": False,
            "confirmation_required": confirmation_result is not None,
        }

    async def _confirmation_llm(self, state: MultiturnGraphState) -> dict[str, Any]:
        proposal = mutation_confirmation_from_state(state)
        messages = build_chat_messages(
            mutation_confirmation_prompt(),
            {
                "original_request": state["request_payload"].get("message"),
                "mutation_confirmation": proposal,
            },
        )
        ai_message = await traced_model_ainvoke(
            self.provider.chat_model(),
            messages,
            name="multiturn_chat.mutation_confirmation",
            prompt_version_id=PROMPT_VERSION_ID,
        )
        native_tool_calls = tool_calls_from_ai_message(ai_message)
        output = normalize_mutation_confirmation_output(model_output_from_ai_message(ai_message))
        serialized_tool_calls = output.get("tool_calls")
        if native_tool_calls or isinstance(output.get("tool_call"), dict) or (isinstance(serialized_tool_calls, list) and serialized_tool_calls):
            raise AgentExecutionError(
                "mutation_confirmation_finalizer_returned_tool_calls",
                error_type="llm_output_validation_failed",
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                decision_type="mutation_confirmation_required",
            )
        return {
            "confirmation_ai_message": ai_message,
            "confirmation_model_output": output,
        }

    def _validate_enabled_delegation_calls(
        self,
        trace_id: str,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        if self.nutrition_recommendation_enabled:
            return
        if any(str(tool_call.get("name") or "") == DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT for tool_call in tool_calls):
            raise AgentExecutionError(
                "nutrition_recommendation_agent_disabled",
                error_type="disabled_agent_delegation",
                trace_id=trace_id,
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                decision_type="delegation",
                retryable=False,
            )

    def _validate_enabled_side_effect_calls(
        self,
        trace_id: str,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        if self.medication_side_effect_enabled:
            return
        if any(
            is_medication_side_effect_feature_call(tool_call)
            for tool_call in tool_calls
        ):
            raise AgentExecutionError(
                "medication_side_effect_feature_disabled",
                error_type="disabled_feature_tool_call",
                trace_id=trace_id,
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                decision_type="tool_call",
                retryable=False,
            )
    async def _run_delegation(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        delegated_call: dict[str, Any],
        *,
        continuation_tool_calls: list[dict[str, Any]] | None = None,
    ) -> AgentResponse:
        target = delegation_target(delegated_call)
        if target == "medication_agent":
            if continuation_tool_calls:
                return await self.medication_agent.continue_with_tool_calls(
                    trace_id,
                    request_payload,
                    tool_calls=continuation_tool_calls,
                )
            return await self.medication_agent.run(
                trace_id,
                request_payload,
            )
        if target == "nutrition_management_agent":
            return await self.nutrition_management_agent.run(trace_id, request_payload)
        if target == "nutrition_recommendation_agent":
            self._validate_enabled_delegation_calls(trace_id, [delegated_call])
            return await self.nutrition_recommendation_agent.run(trace_id, request_payload)
        raise ValueError(f"unsupported_delegation_target:{target or 'unknown'}")
