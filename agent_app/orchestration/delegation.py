from __future__ import annotations

from typing import Any

from agent_app.tools.names import (
    DELEGATE_TO_MEDICATION_AGENT,
    DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
    DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
    DELEGATION_TOOL_NAMES,
    MODEL_VISIBLE_TOOL_METADATA,
)


def delegation_tools_payload() -> list[dict[str, Any]]:
    return [
        _delegation_tool(
            DELEGATE_TO_MEDICATION_AGENT,
            "Delegate medication adherence, dose-taking, and side-effect triage work to the MedicationAgent.",
        ),
        _delegation_tool(
            DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
            "Delegate confirmed meal logging, food search, current meal record checks, meal history, nutrition summaries, meal or food updates/deletes, post-delete verification, and nutrition preference management.",
        ),
        _delegation_tool(
            DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
            "Delegate diet recommendation requests that need today's intake, saved preferences, and nutrition constraints.",
        ),
    ]


def is_delegation_tool_call(tool_call: dict[str, Any]) -> bool:
    return str(tool_call.get("name") or "") in DELEGATION_TOOL_NAMES


def delegation_target(tool_call: dict[str, Any]) -> str:
    name = str(tool_call.get("name") or "")
    if name == DELEGATE_TO_MEDICATION_AGENT:
        return "medication_agent"
    if name == DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT:
        return "nutrition_management_agent"
    if name == DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT:
        return "nutrition_recommendation_agent"
    return ""


def delegation_reason(tool_call: dict[str, Any]) -> str:
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    return str(arguments.get("reason") or arguments.get("task") or "").strip()


def _delegation_tool(name: str, description: str) -> dict[str, Any]:
    args_schema = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Short task description for the specialist agent."},
            "reason": {"type": "string", "description": "Why the supervisor chose this specialist."},
        },
        "required": ["task"],
        "additionalProperties": True,
    }
    metadata = MODEL_VISIBLE_TOOL_METADATA[name]
    return {
        "name": name,
        "title": name,
        "description": description,
        "inputSchema": args_schema,
        "args_schema": args_schema,
        **metadata,
        "_meta": metadata,
    }
