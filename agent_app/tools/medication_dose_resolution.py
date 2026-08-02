from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Literal, TypedDict

from agent_app.llm.context import context_value


class DoseTargetResolution(TypedDict, total=False):
    kind: Literal[
        "resolved",
        "already_taken",
        "selection_required",
        "clarification_required",
    ]
    dose_event_id: str
    candidate: dict[str, Any]
    candidates: list[dict[str, Any]]
    selection_request: dict[str, Any]


_DOSE_SUFFIX_RE = re.compile(
    r"\d+(?:[.,]\d+)?(?:mg|mcg|μg|g|ml|정|캡슐|포|회)",
    re.IGNORECASE,
)
_NON_NAME_RE = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)
_SLOT_WORDS = ("아침", "점심", "저녁", "취침", "밤")


def resolve_dose_target(
    arguments: dict[str, Any],
    *,
    payload: dict[str, Any],
) -> DoseTargetResolution:
    """Resolve one trusted current dose event before approval is prepared.

    The model may describe a medication by name, but it never gets to invent
    the record identifier. Exact and high-confidence unique matches proceed;
    ambiguous matches become a server-owned selection step.
    """

    supplied_id = str(arguments.get("dose_event_id") or "").strip()
    events = _trusted_dose_events(payload)
    if supplied_id:
        matching = [
            event
            for event in events
            if str(event.get("dose_event_id") or event.get("id") or "")
            == supplied_id
        ]
        if len(matching) == 1:
            return _resolved(matching[0])
        return {
            "kind": "clarification_required",
        }

    medication_reference = str(
        arguments.get("medication_name") or ""
    ).strip()
    user_message = str(payload.get("message") or "").strip()
    candidates = _ranked_candidates(
        events,
        medication_reference=medication_reference,
        user_message=user_message,
    )
    if not candidates:
        return {
            "kind": "clarification_required",
        }

    top = candidates[0]
    top_score = float(top.pop("_match_score", 0.0))
    second_score = (
        float(candidates[1].get("_match_score") or 0.0)
        if len(candidates) > 1
        else 0.0
    )
    for candidate in candidates[1:]:
        candidate.pop("_match_score", None)

    unique_confident_match = (
        top_score >= 0.80
        and (
            len(candidates) == 1
            or top_score - second_score >= 0.08
        )
    )
    if unique_confident_match:
        return _resolved(top)

    selection_candidates = [
        _public_candidate(candidate)
        for candidate in candidates
    ]
    _make_selection_values_unique(selection_candidates)
    return {
        "kind": "selection_required",
        "candidates": selection_candidates,
        "selection_request": {
            "message_title": "복약 항목 선택",
            "text": "복용한 약과 시간에 해당하는 항목을 선택해 주세요.",
            "tables": None,
            "selections": [
                str(candidate["selection_value"])
                for candidate in selection_candidates
            ],
            "inputs": None,
        },
    }


def _trusted_dose_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = context_value(payload, "trusted_patient_context")
    if not isinstance(snapshot, dict):
        return []
    today = snapshot.get("today_medication")
    if not isinstance(today, dict):
        return []
    events = today.get("dose_events")
    if not isinstance(events, list):
        return []
    return [
        dict(event)
        for event in events
        if isinstance(event, dict)
        and str(
            event.get("dose_event_id") or event.get("id") or ""
        ).strip()
    ]


def _ranked_candidates(
    events: list[dict[str, Any]],
    *,
    medication_reference: str,
    user_message: str,
) -> list[dict[str, Any]]:
    if not events:
        return []
    reference = _normalized_name(medication_reference)
    normalized_message = _normalized_name(user_message)
    explicit_slot = next(
        (slot for slot in _SLOT_WORDS if slot in user_message),
        "",
    )
    scored: list[dict[str, Any]] = []
    for event in events:
        slot_label = str(event.get("slot_label") or "")
        if explicit_slot and explicit_slot not in slot_label:
            continue
        event_name = _normalized_name(
            str(event.get("medication_name") or "")
        )
        if not event_name:
            continue
        if reference:
            score = _name_score(reference, event_name)
        else:
            score = _message_name_score(
                event_name,
                normalized_message,
            )
            if explicit_slot:
                score = max(score, 0.76)
            elif len(events) == 1:
                score = max(score, 0.82)
        if score < 0.68:
            continue
        candidate = _public_candidate(event)
        candidate["_match_score"] = round(score, 6)
        scored.append(candidate)
    scored.sort(
        key=lambda item: (
            float(item.get("_match_score") or 0.0),
            str(item.get("scheduled_for") or ""),
        ),
        reverse=True,
    )
    return scored


def _resolved(event: dict[str, Any]) -> DoseTargetResolution:
    candidate = _public_candidate(event)
    return {
        "kind": (
            "already_taken"
            if str(candidate.get("status") or "") == "taken"
            else "resolved"
        ),
        "dose_event_id": str(candidate["dose_event_id"]),
        "candidate": candidate,
    }


def _public_candidate(event: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "dose_event_id": str(
            event.get("dose_event_id") or event.get("id") or ""
        ),
        "medication_name": str(
            event.get("medication_name") or ""
        ).strip(),
        "slot_label": str(event.get("slot_label") or "").strip(),
        "scheduled_for": str(event.get("scheduled_for") or "").strip(),
        "status": str(event.get("status") or "").strip(),
    }
    candidate["selection_value"] = _selection_value(candidate)
    return candidate


def _selection_value(candidate: dict[str, Any]) -> str:
    medication_name = str(candidate.get("medication_name") or "약")
    slot_label = str(candidate.get("slot_label") or "").strip()
    scheduled_for = str(candidate.get("scheduled_for") or "").strip()
    time_text = ""
    if "T" in scheduled_for:
        time_text = scheduled_for.split("T", 1)[1][:5]
    elif len(scheduled_for) >= 5 and ":" in scheduled_for:
        time_text = scheduled_for[-8:-3]
    details = " ".join(
        value for value in (slot_label, time_text) if value
    )
    return (
        f"{medication_name} · {details}"
        if details
        else medication_name
    )


def _make_selection_values_unique(
    candidates: list[dict[str, Any]],
) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        value = str(candidate.get("selection_value") or "")
        grouped.setdefault(value, []).append(candidate)
    for matching in grouped.values():
        if len(matching) < 2:
            continue
        for index, candidate in enumerate(matching, start=1):
            candidate["selection_value"] = (
                f"{candidate['selection_value']} ({index})"
            )


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = _DOSE_SUFFIX_RE.sub("", normalized)
    return _NON_NAME_RE.sub("", normalized)


def _name_score(reference: str, candidate: str) -> float:
    if reference == candidate:
        return 1.0
    if reference in candidate or candidate in reference:
        shorter = min(len(reference), len(candidate))
        longer = max(len(reference), len(candidate))
        return 0.92 + 0.08 * (shorter / max(1, longer))
    return SequenceMatcher(None, reference, candidate).ratio()


def _message_name_score(candidate: str, message: str) -> float:
    if not candidate or not message:
        return 0.0
    if candidate in message:
        return 1.0
    width = len(candidate)
    if len(message) <= width:
        return SequenceMatcher(None, candidate, message).ratio()
    return max(
        SequenceMatcher(
            None,
            candidate,
            message[index : index + width],
        ).ratio()
        for index in range(len(message) - width + 1)
    )
