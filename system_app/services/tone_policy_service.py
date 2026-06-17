from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object
from system_app.models import ChatMessage, DoseEvent, Notification

TONE_KEYS = {"empathy", "persuasion", "practical", "warning_soft", "side_effect_check"}
PATTERN_CODES = {"A", "B", "C", "D", "E"}
TONE_MESSAGE_CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "missed_dose_tone_messages.csv"

PATTERN_DEFAULT_TONES: dict[str, str] = {
    "A": "empathy",
    "B": "persuasion",
    "C": "side_effect_check",
    "D": "warning_soft",
    "E": "warning_soft",
}

TONE_INTENSITY: dict[str, int] = {
    "empathy": 1,
    "persuasion": 1,
    "practical": 2,
    "side_effect_check": 2,
    "warning_soft": 3,
}

TONE_ESCALATION: dict[str, str] = {
    "empathy": "practical",
    "persuasion": "practical",
    "practical": "warning_soft",
}

TONE_MESSAGE_CATALOG: dict[tuple[str, str], str] = {
    ("A", "empathy"): "잠시 놓치신 것 같아요. 지금 상태를 알려주세요.",
    ("A", "practical"): "지금 1분만 기록해볼까요? 상태를 알려주세요.",
    ("A", "warning_soft"): "기록 확인이 필요해요. 지금 상태를 알려주세요.",
    ("A", "side_effect_check"): "불편한 증상이 있나요? 상태를 먼저 알려주세요.",
    ("B", "persuasion"): "복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
    ("B", "practical"): "지금 바로 기록해볼까요? 짧게 확인할게요.",
    ("B", "warning_soft"): "기록이 이어서 비어 있어요. 함께 확인할게요.",
    ("B", "empathy"): "괜찮아요. 지금 상황을 함께 확인해볼게요.",
    ("B", "side_effect_check"): "불편한 증상이 있나요? 상태를 먼저 알려주세요.",
    ("C", "side_effect_check"): "불편한 증상이 있나요? 상태를 먼저 알려주세요.",
    ("D", "warning_soft"): "며칠째 기록이 비어 있어요. 의료진과 함께 확인할게요.",
    ("D", "practical"): "오늘 기록을 먼저 확인해요. 상태를 알려주세요.",
    ("D", "empathy"): "부담을 줄여 확인해볼게요. 지금 상태를 알려주세요.",
    ("D", "side_effect_check"): "불편한 증상이 있나요? 상태를 먼저 알려주세요.",
    ("E", "warning_soft"): "최근 기록이 많이 비어 있어요. 의료진과 함께 확인할게요.",
    ("E", "practical"): "최근 기록을 같이 정리해요. 지금 상태를 알려주세요.",
    ("E", "empathy"): "부담을 줄여 확인해볼게요. 지금 상태를 알려주세요.",
    ("E", "side_effect_check"): "불편한 증상이 있나요? 상태를 먼저 알려주세요.",
}

TONE_FALLBACK_MESSAGES: dict[str, str] = {
    "empathy": TONE_MESSAGE_CATALOG[("A", "empathy")],
    "persuasion": TONE_MESSAGE_CATALOG[("B", "persuasion")],
    "practical": TONE_MESSAGE_CATALOG[("B", "practical")],
    "side_effect_check": TONE_MESSAGE_CATALOG[("C", "side_effect_check")],
    "warning_soft": TONE_MESSAGE_CATALOG[("B", "warning_soft")],
}


@dataclass(frozen=True)
class ToneMessageCandidate:
    pattern_code: str
    tone_key: str
    variant_key: str
    priority: int
    message: str
    source: str


@dataclass(frozen=True)
class TonePolicyDecision:
    pattern_code: str
    tone_key: str
    policy_variant: str
    intensity: int
    message: str
    message_variant: str
    message_catalog_source: str
    selection_reason: str
    slot_label: str

    def to_metadata(self) -> dict[str, Any]:
        return asdict(self)


_catalog_cache_mtime: float | None = None
_catalog_cache: dict[tuple[str, str], list[ToneMessageCandidate]] | None = None


def message_for_pattern_tone(pattern_code: str, tone_key: str) -> str:
    return message_candidates_for_pattern_tone(pattern_code, tone_key)[0].message


def default_message_for_pattern(pattern_code: str) -> str:
    return message_for_pattern_tone(pattern_code, PATTERN_DEFAULT_TONES[pattern_code])


def message_candidates_for_pattern_tone(pattern_code: str, tone_key: str) -> list[ToneMessageCandidate]:
    catalog = _load_message_catalog()
    candidates = catalog.get((pattern_code, tone_key))
    if candidates:
        return candidates
    fallback_message = TONE_FALLBACK_MESSAGES.get(tone_key, "")
    return [
        ToneMessageCandidate(
            pattern_code=pattern_code,
            tone_key=tone_key,
            variant_key="v1",
            priority=10,
            message=fallback_message,
            source="fallback",
        )
    ]


def all_tone_message_candidates() -> list[ToneMessageCandidate]:
    return [candidate for candidates in _load_message_catalog().values() for candidate in candidates]


def select_tone_policy(
    session: Session,
    event: DoseEvent,
    pattern_code: str,
    *,
    lookback_days: int = 14,
) -> TonePolicyDecision:
    if pattern_code == "C":
        return _decision(session, event, pattern_code, "side_effect_check", event.slot_label, "pattern_c_side_effect_priority", lookback_days)

    reply_signal = _recent_reply_signal_tone(session, event, lookback_days=lookback_days)
    if reply_signal:
        tone_key, reason = reply_signal
        return _decision(session, event, pattern_code, tone_key, event.slot_label, reason, lookback_days)

    successful_tone = _recent_take_success_tone(session, event, lookback_days=lookback_days)
    if successful_tone:
        return _decision(session, event, pattern_code, successful_tone, event.slot_label, "reuse_recent_take_success_tone", lookback_days)

    tone_key = PATTERN_DEFAULT_TONES.get(pattern_code, "persuasion")
    reason = f"default_for_pattern_{pattern_code.lower()}"
    while tone_key in TONE_ESCALATION:
        failed_tone = tone_key
        if _failed_reaction_count(session, event, failed_tone, lookback_days=lookback_days) < 2:
            break
        tone_key = TONE_ESCALATION[failed_tone]
        reason = f"escalated_after_two_{failed_tone}_failures"
    return _decision(session, event, pattern_code, tone_key, event.slot_label, reason, lookback_days)


def _decision(
    session: Session,
    event: DoseEvent,
    pattern_code: str,
    tone_key: str,
    slot_label: str,
    selection_reason: str,
    lookback_days: int,
) -> TonePolicyDecision:
    candidate = _select_message_candidate(session, event, pattern_code, tone_key, lookback_days=lookback_days)
    return TonePolicyDecision(
        pattern_code=pattern_code,
        tone_key=tone_key,
        policy_variant=f"missed_dose.{tone_key}.{candidate.variant_key}",
        intensity=TONE_INTENSITY.get(tone_key, 1),
        message=candidate.message,
        message_variant=candidate.variant_key,
        message_catalog_source=candidate.source,
        selection_reason=selection_reason,
        slot_label=slot_label,
    )


def _recent_take_success_tone(session: Session, event: DoseEvent, *, lookback_days: int) -> str:
    for row in _recent_persona_reaction_rows(session, event, lookback_days=lookback_days):
        tone_policy = row["tone_policy"]
        reaction = row["persona_reaction"]
        tone_key = str(tone_policy.get("tone_key") or "")
        if tone_key not in TONE_KEYS:
            continue
        if reaction.get("action") == "take" and reaction.get("success") is True:
            return tone_key
    return ""


def _failed_reaction_count(session: Session, event: DoseEvent, tone_key: str, *, lookback_days: int) -> int:
    count = 0
    for row in _recent_persona_reaction_rows(session, event, lookback_days=lookback_days):
        tone_policy = row["tone_policy"]
        reaction = row["persona_reaction"]
        if tone_policy.get("tone_key") != tone_key:
            continue
        if reaction.get("success") is not True:
            count += 1
    return count


def _recent_persona_reaction_rows(session: Session, event: DoseEvent, *, lookback_days: int) -> list[dict[str, Any]]:
    cutoff = event.scheduled_for - timedelta(days=lookback_days)
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.category == "missed_dose", ChatMessage.patient_id == event.patient_id)
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
        .limit(100)
    ).all()
    matches: list[dict[str, Any]] = []
    for message in rows:
        metadata = parse_json_object(message.metadata_json)
        tone_policy = metadata.get("tone_policy") if isinstance(metadata.get("tone_policy"), dict) else {}
        reaction = metadata.get("persona_reaction") if isinstance(metadata.get("persona_reaction"), dict) else {}
        if not tone_policy or not reaction:
            continue
        if _message_slot_label(session, message, tone_policy) != event.slot_label:
            continue
        observed_at = _reaction_observed_at(reaction, message.created_at)
        if observed_at < cutoff:
            continue
        matches.append(
            {
                "message": message,
                "tone_policy": tone_policy,
                "persona_reaction": reaction,
                "observed_at": observed_at,
            }
        )
    matches.sort(key=lambda item: (item["observed_at"], item["message"].id), reverse=True)
    return matches


def _select_message_candidate(
    session: Session,
    event: DoseEvent,
    pattern_code: str,
    tone_key: str,
    *,
    lookback_days: int,
) -> ToneMessageCandidate:
    candidates = message_candidates_for_pattern_tone(pattern_code, tone_key)
    if len(candidates) <= 1:
        return candidates[0]
    recent_variants, recent_messages = _recent_message_variant_usage(
        session,
        event,
        pattern_code,
        tone_key,
        lookback_days=lookback_days,
    )
    for candidate in candidates:
        if candidate.variant_key not in recent_variants and candidate.message not in recent_messages:
            return candidate
    return candidates[0]


def _recent_message_variant_usage(
    session: Session,
    event: DoseEvent,
    pattern_code: str,
    tone_key: str,
    *,
    lookback_days: int,
) -> tuple[set[str], set[str]]:
    cutoff = event.scheduled_for - timedelta(days=lookback_days)
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.category == "missed_dose", ChatMessage.patient_id == event.patient_id)
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
        .limit(100)
    ).all()
    variants: set[str] = set()
    messages: set[str] = set()
    for message in rows:
        metadata = parse_json_object(message.metadata_json)
        tone_policy = metadata.get("tone_policy") if isinstance(metadata.get("tone_policy"), dict) else {}
        if not tone_policy:
            continue
        if message.created_at < cutoff:
            continue
        if tone_policy.get("pattern_code") != pattern_code or tone_policy.get("tone_key") != tone_key:
            continue
        variant_key = str(tone_policy.get("message_variant") or "")
        if variant_key:
            variants.add(variant_key)
        if message.content:
            messages.add(message.content)
    return variants, messages


def _message_slot_label(session: Session, message: ChatMessage, tone_policy: dict[str, Any]) -> str:
    slot_label = str(tone_policy.get("slot_label") or "")
    if slot_label:
        return slot_label
    if message.related_dose_event_id is None:
        return ""
    event = session.get(DoseEvent, message.related_dose_event_id)
    return event.slot_label if event is not None else ""


def _reaction_observed_at(reaction: dict[str, Any], fallback: datetime) -> datetime:
    raw = reaction.get("observed_at")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return fallback
    return fallback


def _recent_reply_signal_tone(session: Session, event: DoseEvent, *, lookback_days: int) -> tuple[str, str] | None:
    cutoff = event.scheduled_for - timedelta(days=lookback_days)
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.notification_type == "conversation_alert",
            Notification.related_dose_event_id.is_not(None),
            Notification.visible_at >= cutoff,
        )
        .order_by(desc(Notification.visible_at), desc(Notification.id))
        .limit(100)
    ).all()
    for notification in rows:
        metadata = parse_json_object(notification.metadata_json)
        if metadata.get("category") != "missed_dose":
            continue
        previous_event = session.get(DoseEvent, notification.related_dose_event_id)
        if previous_event is None or previous_event.slot_label != event.slot_label:
            continue
        understanding = metadata.get("missed_dose_reply_understanding")
        if not isinstance(understanding, dict):
            continue
        signals = understanding.get("policy_signals") if isinstance(understanding.get("policy_signals"), dict) else {}
        avoid_tones = _reply_signal_avoid_tones(signals)
        if signals.get("needs_side_effect_check") is True:
            return "side_effect_check", "recent_reply_side_effect_signal"
        if signals.get("suppress_candidate") is True or signals.get("needs_support_first") is True:
            if "empathy" not in avoid_tones:
                return "empathy", "recent_reply_support_first_signal"
        prefer_tone = str(signals.get("prefer_tone") or "")
        if prefer_tone in TONE_KEYS and prefer_tone not in avoid_tones:
            return prefer_tone, "recent_reply_prefer_tone_signal"
    return None


def _reply_signal_avoid_tones(signals: dict[str, Any]) -> set[str]:
    raw = signals.get("avoid_tones")
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = [str(item) for item in raw]
    else:
        candidates = []
    return {tone for tone in candidates if tone in TONE_KEYS}


def _load_message_catalog() -> dict[tuple[str, str], list[ToneMessageCandidate]]:
    global _catalog_cache_mtime, _catalog_cache
    mtime = _catalog_mtime()
    if _catalog_cache is not None and _catalog_cache_mtime == mtime:
        return _catalog_cache
    catalog = _load_csv_message_catalog()
    if not catalog:
        catalog = {}
    fallback = _fallback_message_catalog()
    for key, candidates in fallback.items():
        catalog.setdefault(key, candidates)
    for key in list(catalog.keys()):
        catalog[key] = sorted(catalog[key], key=lambda row: (row.priority, row.variant_key))
    _catalog_cache = catalog
    _catalog_cache_mtime = mtime
    return catalog


def _load_csv_message_catalog() -> dict[tuple[str, str], list[ToneMessageCandidate]]:
    if not TONE_MESSAGE_CSV_PATH.exists():
        return {}
    catalog: dict[tuple[str, str], list[ToneMessageCandidate]] = {}
    with TONE_MESSAGE_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            candidate = _csv_row_candidate(row)
            if candidate is None:
                continue
            catalog.setdefault((candidate.pattern_code, candidate.tone_key), []).append(candidate)
    return catalog


def _csv_row_candidate(row: dict[str, str]) -> ToneMessageCandidate | None:
    if not _csv_bool(row.get("active"), default=True):
        return None
    pattern_code = str(row.get("pattern_code") or "").strip().upper()
    tone_key = str(row.get("tone_key") or "").strip()
    variant_key = str(row.get("variant_key") or "").strip() or "v1"
    message = str(row.get("message") or "").strip()
    if pattern_code not in PATTERN_CODES or tone_key not in TONE_KEYS or not message:
        return None
    return ToneMessageCandidate(
        pattern_code=pattern_code,
        tone_key=tone_key,
        variant_key=variant_key,
        priority=_csv_int(row.get("priority"), default=10),
        message=message,
        source=str(TONE_MESSAGE_CSV_PATH),
    )


def _fallback_message_catalog() -> dict[tuple[str, str], list[ToneMessageCandidate]]:
    return {
        key: [
            ToneMessageCandidate(
                pattern_code=key[0],
                tone_key=key[1],
                variant_key="v1",
                priority=10,
                message=message,
                source="code_fallback",
            )
        ]
        for key, message in TONE_MESSAGE_CATALOG.items()
    }


def _catalog_mtime() -> float | None:
    try:
        return TONE_MESSAGE_CSV_PATH.stat().st_mtime
    except OSError:
        return None


def _csv_bool(value: str | None, *, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_int(value: str | None, *, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
