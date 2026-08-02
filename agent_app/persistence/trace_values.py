from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_app.persistence.models import AgentRunStep


def token_usage(structured: dict[str, Any]) -> dict[str, int]:
    observations = dict_list(structured.get("model_call_observations"))
    if observations:
        return {
            "input_tokens": sum(
                nonnegative_int(
                    (
                        observation.get("usage_details")
                        if isinstance(
                            observation.get("usage_details"), dict
                        )
                        else {}
                    ).get("input")
                )
                for observation in observations
            ),
            "output_tokens": sum(
                nonnegative_int(
                    (
                        observation.get("usage_details")
                        if isinstance(
                            observation.get("usage_details"), dict
                        )
                        else {}
                    ).get("output")
                )
                for observation in observations
            ),
        }

    root_usage = (
        structured.get("token_usage")
        if isinstance(structured.get("token_usage"), dict)
        else {}
    )
    if root_usage:
        return {
            "input_tokens": nonnegative_int(
                root_usage.get("input_tokens")
            ),
            "output_tokens": nonnegative_int(
                root_usage.get("output_tokens")
            ),
        }

    candidates = [
        value
        for key in (
            "model_output",
            "final_model_output",
            "supervisor_model_output",
            "supervisor_final_model_output",
        )
        if isinstance((value := structured.get(key)), dict)
    ]
    input_tokens = 0
    output_tokens = 0
    seen: set[int] = set()
    for candidate in candidates:
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        raw = candidate.get("token_usage")
        usage = raw if isinstance(raw, dict) else {}
        input_tokens += nonnegative_int(
            usage.get("input_tokens") or candidate.get("input_tokens")
        )
        output_tokens += nonnegative_int(
            usage.get("output_tokens") or candidate.get("output_tokens")
        )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def next_sequence(session: Session, trace_id: str) -> int:
    return (
        session.scalar(
            select(AgentRunStep.sequence)
            .where(AgentRunStep.trace_id == trace_id)
            .order_by(AgentRunStep.sequence.desc())
            .limit(1)
        )
        or 0
    ) + 1


def attempt_root_observation_id(
    session: Session,
    trace_id: str,
    trace_attempt_number: int,
) -> str:
    return str(
        session.scalar(
            select(AgentRunStep.observation_id)
            .where(
                AgentRunStep.trace_id == trace_id,
                AgentRunStep.trace_attempt_number == trace_attempt_number,
                AgentRunStep.step_type == "request_ingress",
            )
            .order_by(AgentRunStep.sequence.desc())
            .limit(1)
        )
        or ""
    )


def attempt_started_at(
    session: Session,
    trace_id: str,
    trace_attempt_number: int,
) -> datetime | None:
    return session.scalar(
        select(AgentRunStep.started_at)
        .where(
            AgentRunStep.trace_id == trace_id,
            AgentRunStep.trace_attempt_number == trace_attempt_number,
            AgentRunStep.step_type == "request_ingress",
        )
        .order_by(AgentRunStep.sequence.desc())
        .limit(1)
    )


def duration_ms(
    started_at: datetime | None,
    completed_at: datetime,
) -> int:
    if started_at is None:
        return 0
    return max(
        0,
        round((completed_at - started_at).total_seconds() * 1000),
    )


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except ValueError:
        return None


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def json_value(value: Any, *, default: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)):
        return value
    return default


def observation_id() -> str:
    return uuid.uuid4().hex


def safe_error_code(value: Any) -> str:
    text = str(value or "").strip()
    if (
        text
        and len(text) <= 80
        and all(
            character.isascii()
            and (character.isalnum() or character in "_:-.")
            for character in text
        )
    ):
        return text
    return "TOOL_EXECUTION_ERROR" if text else ""


def dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def side_effect_level(metadata: dict[str, str]) -> str:
    mutability = metadata.get("mutability", "")
    if mutability in {"write", "delete"}:
        return "approval_required"
    if mutability == "propose":
        return "deferred"
    return "read_only"
