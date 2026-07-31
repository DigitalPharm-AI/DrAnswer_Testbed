from __future__ import annotations

import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from agent_app.jobs.tasks import enqueue_async_task
from shared.tool_names import PROPOSE_NOTIFICATION_POLICY
from shared.async_v13_contracts import (
    NotificationPolicyChangeProposalRequest,
    NotificationPolicyProposal,
)
from shared.public_ids import new_public_id, require_public_id
from shared.schemas import AgentResponse, NotificationPolicyDelta
from shared.time_utils import as_aware_utc, as_naive_utc

DAILY_PATTERN_TIMEZONE = ZoneInfo("Asia/Seoul")
DAILY_PATTERN_ANALYSIS_TASK = "daily_pattern_analysis"
DAILY_PATTERN_DELIVERY_TASK = "notification_policy_proposal_delivery"
DAILY_PATTERN_DEFAULT_DELIVERY_TIME = time(8, 30)


class DailyPatternProposalPriorityUndefined(ValueError):
    """Multiple distinct candidates cannot be ranked by the current schema."""


def v13_proposal_delivery_run_after(*, received_at: datetime) -> datetime:
    """Return the v1.3 default 08:30 KST proposal delivery slot.

    Backend owns the daily 02:00 trigger. A delayed Backend request can yield a
    timestamp in the past; the durable queue then delivers immediately.
    """

    local_received = as_aware_utc(received_at).astimezone(
        DAILY_PATTERN_TIMEZONE
    )
    delivery_local = datetime.combine(
        local_received.date(),
        DAILY_PATTERN_DEFAULT_DELIVERY_TIME,
        tzinfo=DAILY_PATTERN_TIMEZONE,
    )
    return as_naive_utc(delivery_local)


def enqueue_proposal_delivery(
    session: Session,
    *,
    analysis_task_payload: dict,
    callback_context: dict,
    response: AgentResponse,
) -> bool:
    """Stage the minimal callback body produced by an analysis task."""

    patient_id = require_public_id(
        str(analysis_task_payload.get("patient_id") or ""),
        "patient",
    )
    analysis_date = date.fromisoformat(
        str(analysis_task_payload.get("analysis_date") or ""),
    )
    delivery_run_after = datetime.fromisoformat(
        str(analysis_task_payload.get("delivery_run_after") or ""),
    )
    if delivery_run_after.tzinfo is not None:
        delivery_run_after = as_naive_utc(delivery_run_after)
    proposal = _single_proposal(response)
    if proposal is None:
        return False
    request_id = new_public_id("request")
    callback_body = NotificationPolicyChangeProposalRequest(
        request_id=request_id,
        patient_id=patient_id,
        proposed_policy=proposal["proposed_policy"],
        reason=proposal["reason"],
    )
    _task, created = enqueue_async_task(
        session,
        request_id=request_id,
        deduplication_key=(
            "notification-policy-proposal:"
            f"{patient_id}:{analysis_date.isoformat()}"
        ),
        task_type=DAILY_PATTERN_DELIVERY_TASK,
        payload=callback_body.model_dump(mode="json"),
        callback_context=callback_context,
        run_after=delivery_run_after,
    )
    return created


def _single_proposal(
    response: AgentResponse,
) -> dict[str, object] | None:
    raw_calls = response.structured_payload.get("tool_calls")
    if not isinstance(raw_calls, list):
        return None
    candidates: dict[str, dict[str, object]] = {}
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            continue
        if str(raw_call.get("name") or "") != PROPOSE_NOTIFICATION_POLICY:
            continue
        arguments = raw_call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        delta = NotificationPolicyDelta.model_validate(arguments)
        raw_delta = delta.model_dump(mode="json")
        proposed_policy = NotificationPolicyProposal.model_validate(
            {
                field_name: raw_delta[field_name]
                for field_name in NotificationPolicyProposal.model_fields
                if raw_delta.get(field_name) is not None
            }
        )
        candidate = {
            "proposed_policy": proposed_policy,
            "reason": delta.reason,
        }
        fingerprint = json.dumps(
            {
                "proposed_policy": proposed_policy.model_dump(mode="json"),
                "reason": delta.reason,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidates[fingerprint] = candidate
    if not candidates:
        return None
    if len(candidates) > 1:
        raise DailyPatternProposalPriorityUndefined(
            "daily_pattern_multiple_proposals_require_priority_contract"
        )
    return next(iter(candidates.values()))
