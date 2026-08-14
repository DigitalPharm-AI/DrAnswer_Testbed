from __future__ import annotations

import asyncio

import pytest

from agent_app.agents import multiturn_chat as multiturn_chat_module
from agent_app.agents.medication import MedicationAgent
from agent_app.agents.multiturn_graph import route_after_llm
from agent_app.errors import AgentExecutionError
from agent_app.llm.prompts import medication_agent_prompt, multiturn_chat_prompt
from agent_app.llm.side_effect_guard import SIDE_EFFECT_FEATURE_UNAVAILABLE_MESSAGE
from agent_app.orchestration.delegation import delegation_tools_payload
from agent_app.tools.runtime import ToolRuntime
from shared.tool_names import (
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    DELEGATE_TO_MEDICATION_AGENT,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    MEDICATION_SIDE_EFFECT_FEATURE_TOOLS,
    REQUEST_RECORD_APPROVAL,
)
from tests.support.llm import NativeChatProvider


class NoopProvider(NativeChatProvider):
    async def model_output(self, system_prompt, user_payload):
        return {"message": "?? ??? ?? ??? ???? ????."}


class UnexpectedExecutor:
    async def execute_tool_call(self, *args, **kwargs):
        raise AssertionError("disabled side-effect Tool must not execute")


def _agent(*, enabled: bool) -> MedicationAgent:
    return MedicationAgent(
        NoopProvider(),
        ToolRuntime(None),
        medication_side_effect_enabled=enabled,
    )


def test_disabled_mode_removes_all_side_effect_tools_from_medication_binding():
    agent = _agent(enabled=False)
    bound_tools = set(agent.graph_runner.tool_names)

    assert bound_tools.isdisjoint(MEDICATION_SIDE_EFFECT_FEATURE_TOOLS)
    assert GET_MEDICATION_DOSE_STATUS in bound_tools
    assert REQUEST_RECORD_APPROVAL in bound_tools
    assert agent.graph_runner.force_ae_after_positive_lookup is False


def test_enabled_mode_restores_existing_side_effect_tool_contract():
    agent = _agent(enabled=True)
    bound_tools = set(agent.graph_runner.tool_names)

    assert GET_MEDICATION_SIDE_EFFECT_ASSESSMENT in bound_tools
    assert GET_SIDE_EFFECT_HISTORY in bound_tools
    assert CREATE_MEDICATION_SIDE_EFFECT_RECORD in bound_tools
    assert GET_PRO_CTCAE_QUESTIONNAIRE not in bound_tools
    assert agent.graph_runner.force_ae_after_positive_lookup is True


@pytest.mark.parametrize(
    "tool_call",
    [
        {
            "name": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
            "arguments": {"symptom_mentions": [{"text": "????"}]},
        },
        {
            "name": REQUEST_RECORD_APPROVAL,
            "arguments": {
                "action_name": CREATE_MEDICATION_SIDE_EFFECT_RECORD,
                "record_arguments": {"symptom_text": "????"},
            },
        },
    ],
)
def test_disabled_mode_rejects_injected_side_effect_calls(tool_call):
    agent = _agent(enabled=False)

    with pytest.raises(AgentExecutionError) as caught:
        asyncio.run(
            agent.continue_with_tool_calls(
                "trace-disabled-side-effect",
                {"message": "???? ????", "context": {}},
                tool_calls=[tool_call],
            )
        )

    assert caught.value.error_type == "disabled_feature_tool_call"
    assert caught.value.retryable is False


def test_disabled_tool_runtime_blocks_side_effect_calls_before_executor():
    runtime = ToolRuntime(
        UnexpectedExecutor(),
        medication_side_effect_enabled=False,
    )

    with pytest.raises(AgentExecutionError):
        asyncio.run(
            runtime.execute(
                [
                    {
                        "name": GET_SIDE_EFFECT_HISTORY,
                        "arguments": {"limit": 10},
                    }
                ],
                trace_id="trace-runtime-disabled-side-effect",
                source_event_type="medication_agent",
                payload={"message": "??? ??? ???"},
            )
        )


def test_disabled_prompts_and_delegation_hide_side_effect_capability():
    supervisor_prompt = multiturn_chat_prompt(medication_side_effect_enabled=False)
    specialist_prompt = medication_agent_prompt(medication_side_effect_enabled=False)
    medication_tool = next(tool for tool in delegation_tools_payload(medication_side_effect_enabled=False) if tool["name"] == DELEGATE_TO_MEDICATION_AGENT)

    assert "do not answer using general knowledge" in supervisor_prompt
    assert "known side effects of a medication" in supervisor_prompt
    assert "Do not assess whether a symptom is a side effect" in specialist_prompt
    assert "side-effect triage" not in medication_tool["description"]
    assert "dose-taking work" in medication_tool["description"]

@pytest.mark.asyncio
async def test_disabled_side_effect_guard_replaces_side_effect_answer(monkeypatch):
    async def classify(*args, **kwargs):
        return True

    monkeypatch.setattr(
        multiturn_chat_module,
        "is_medication_side_effect_request",
        classify,
    )
    agent = multiturn_chat_module.MultiturnChatAgent(
        NoopProvider(),
        ToolRuntime(None),
        medication_side_effect_enabled=False,
    )

    updates = await agent._side_effect_response_guard(
        {
            "request_payload": {"message": "이 증상이 약 부작용일까요?"},
            "current_final_text": "후보 답변에 의학 내용이 포함되었습니다.",
        }
    )

    assert updates["current_final_text"] == SIDE_EFFECT_FEATURE_UNAVAILABLE_MESSAGE
    assert updates["side_effect_guard_checked"] is True


@pytest.mark.asyncio
async def test_disabled_side_effect_guard_preserves_non_side_effect_answer(monkeypatch):
    async def classify(*args, **kwargs):
        return False

    monkeypatch.setattr(
        multiturn_chat_module,
        "is_medication_side_effect_request",
        classify,
    )
    agent = multiturn_chat_module.MultiturnChatAgent(
        NoopProvider(),
        ToolRuntime(None),
        medication_side_effect_enabled=False,
    )

    updates = await agent._side_effect_response_guard(
        {
            "request_payload": {"message": "오늘 약 복용 기록을 알려줘"},
            "current_final_text": "오늘 복약 기록입니다.",
        }
    )

    assert updates["current_final_text"] == "오늘 복약 기록입니다."
    assert updates["side_effect_guard_checked"] is True


def test_disabled_final_text_routes_through_side_effect_guard():
    state = {
        "pending_tool_calls": [],
        "side_effect_guard_checked": False,
        "entry_mode": "standard",
    }

    assert route_after_llm(state) == "side_effect_response_guard"
