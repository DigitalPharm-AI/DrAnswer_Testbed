from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from shared.schemas import ToolCallResult
from shared.tool_confirmations import ConfirmationActionRegistry
from shared.tool_names import GET_PRO_CTCAE_QUESTIONNAIRE
from shared.tool_permissions import (
    permission_denied_result,
    validate_tool_permission,
)


class ToolCallOrigin(StrEnum):
    """Why one Tool call entered the execution boundary."""

    MODEL = "model"
    CLINICAL_CONTINUATION = "clinical_continuation"
    SAFETY_RULE = "safety_rule"
    APPROVED_WRITE = "approved_write"


@dataclass(frozen=True)
class ToolCallContext:
    """Execution metadata for one call, not a precomputed Agent plan."""

    origin: ToolCallOrigin = ToolCallOrigin.MODEL
    prior_call_fingerprints: frozenset[str] = frozenset()


class ToolPolicyGate:
    """Authorize one Tool call immediately before execution."""

    def authorize(
        self,
        tool_call: dict[str, Any],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
        context: ToolCallContext,
    ) -> ToolCallResult | None:
        fingerprint = tool_call_fingerprint(tool_call)
        if fingerprint in context.prior_call_fingerprints:
            tool_name = str(tool_call.get("name") or "unknown")
            return ToolCallResult(
                tool_name=tool_name,
                status="skipped",
                response={"reason": "duplicate_tool_call"},
                error="",
                idempotency_key=(
                    f"{trace_id}:{tool_name}:{source_event_type}:duplicate"
                ),
            )
        if context.origin is ToolCallOrigin.APPROVED_WRITE:
            # The normal schema intentionally exposes business arguments only
            # to the model. A server-injected approved write carries only the
            # short Agent capability key and is validated below.
            reason = self._validate_approved_write(tool_call, payload)
        else:
            reason = validate_tool_permission(
                tool_call,
                source_event_type=source_event_type,
                payload=payload,
            )
        if reason is None and context.origin is ToolCallOrigin.SAFETY_RULE:
            reason = self._validate_safety_rule_call(tool_call)
        if reason is None:
            return None
        return permission_denied_result(
            tool_call,
            trace_id=trace_id,
            source_event_type=source_event_type,
            reason=reason,
        )

    @staticmethod
    def _validate_safety_rule_call(
        tool_call: dict[str, Any],
    ) -> str | None:
        tool_name = str(tool_call.get("name") or "")
        if tool_name != GET_PRO_CTCAE_QUESTIONNAIRE:
            return (
                "safety_rule may only invoke "
                f"{GET_PRO_CTCAE_QUESTIONNAIRE}"
            )
        return None

    @staticmethod
    def _validate_approved_write(
        tool_call: dict[str, Any],
        payload: dict[str, Any],
    ) -> str | None:
        tool_name = str(tool_call.get("name") or "")
        if not ConfirmationActionRegistry.requires_confirmation(tool_name):
            return (
                "approved_write requires a Tool registered for "
                "user confirmation"
            )

        payload_context = (
            payload.get("context")
            if isinstance(payload.get("context"), dict)
            else {}
        )
        approved = payload_context.get("approved_user_action")
        if not isinstance(approved, dict):
            return "approved_write requires approved_user_action context"
        if approved.get("status") != "confirmed":
            return "approved_write requires confirmed user action"
        if str(approved.get("action_name") or "") != tool_name:
            return "approved_write Tool does not match confirmed action"

        approved_arguments = approved.get("arguments")
        tool_arguments = tool_call.get("arguments")
        if not isinstance(approved_arguments, dict):
            return "approved_write confirmed arguments are missing"
        if not isinstance(tool_arguments, dict):
            return "approved_write Tool arguments are missing"
        if approved_arguments != tool_arguments:
            return "approved_write Tool arguments do not match confirmation"
        if set(tool_arguments) != {"approval_key"}:
            return "approved_write requires only approval_key"
        approval_key = str(tool_arguments.get("approval_key") or "").strip()
        if not approval_key.startswith("apv_"):
            return "approved_write approval_key is invalid"
        return None


def tool_call_fingerprint(tool_call: dict[str, Any]) -> str:
    arguments = (
        tool_call.get("arguments")
        if isinstance(tool_call.get("arguments"), dict)
        else {}
    )
    canonical = json.dumps(
        {
            "name": str(tool_call.get("name") or ""),
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
