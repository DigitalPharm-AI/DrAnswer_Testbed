from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object
from system_app.models import DoseEvent, MedicationPlan, Notification
from system_app.services.notification_service import create_notification
from system_app.services.side_effect_reminder_safety import (
    SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
    is_reminder_suppressed_after_side_effect,
)
from system_app.services.tone_policy_service import default_message_for_pattern

CLINICIAN_ESCALATION_CATEGORY = "clinician_escalation"
CLINICIAN_ESCALATION_TYPE = "clinician_escalation"

PATTERN_MESSAGES: dict[str, str] = {
    "A": default_message_for_pattern("A"),
    "B": default_message_for_pattern("B"),
    "C": default_message_for_pattern("C"),
    "D": default_message_for_pattern("D"),
    "E": default_message_for_pattern("E"),
}

PATTERN_LABELS = {
    "A": "단순 망각",
    "B": "습관 미형성",
    "C": "부작용 가능성",
    "D": "장기 연속 미복용",
    "E": "전반적 저조",
}

PATTERN_TONES = {
    "A": "공감과 차분한 상기",
    "B": "루틴 형성 지원",
    "C": "증상 확인과 의료진 상담 권유",
    "D": "강한 주의와 의료진 확인 기록",
    "E": "강한 주의와 재시작 격려",
}

FORBIDDEN_MESSAGE_TERMS = (
    "타목시펜",
    "레트로졸",
    "암",
    "병기",
    "재발 위험",
    "치료 실패",
    "생명",
    "반드시",
    "복용하세요",
)


@dataclass(frozen=True)
class StreakMetrics:
    current_consecutive_missed_days: int
    previous_consecutive_taken_days: int
    slot_scheduled_count: int
    slot_taken_count: int
    slot_missed_count: int
    overall_scheduled_count: int
    overall_taken_count: int
    overall_missed_count: int
    overall_adherence_rate: float
    prescription_day_count: int
    recent_side_effect_keep: bool

    def to_metadata(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AdherencePatternDecision:
    pattern_code: str
    pattern_label: str
    tone: str
    message: str
    reason: str
    streak_metrics: StreakMetrics
    escalation_stub_required: bool = False

    def to_metadata(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["streak_metrics"] = self.streak_metrics.to_metadata()
        return payload


def evaluate_adherence_pattern(session: Session, event: DoseEvent) -> AdherencePatternDecision:
    metrics = build_streak_metrics(session, event)
    code = _pattern_code(metrics)
    return AdherencePatternDecision(
        pattern_code=code,
        pattern_label=PATTERN_LABELS[code],
        tone=PATTERN_TONES[code],
        message=PATTERN_MESSAGES[code],
        reason=_pattern_reason(code, metrics),
        streak_metrics=metrics,
        escalation_stub_required=code in {"D", "E"},
    )


def build_streak_metrics(session: Session, event: DoseEvent) -> StreakMetrics:
    slot_statuses = _slot_day_statuses(session, event)
    current_missed, previous_taken = _streak_counts(slot_statuses)
    slot_scheduled = len(slot_statuses)
    slot_taken = sum(1 for status in slot_statuses if status == "taken")
    slot_missed = sum(1 for status in slot_statuses if status == "missed")
    overall_taken, overall_missed = _overall_counts(session, event)
    overall_scheduled = overall_taken + overall_missed
    adherence_rate = (overall_taken / overall_scheduled) if overall_scheduled else 0.0
    return StreakMetrics(
        current_consecutive_missed_days=current_missed,
        previous_consecutive_taken_days=previous_taken,
        slot_scheduled_count=slot_scheduled,
        slot_taken_count=slot_taken,
        slot_missed_count=slot_missed,
        overall_scheduled_count=overall_scheduled,
        overall_taken_count=overall_taken,
        overall_missed_count=overall_missed,
        overall_adherence_rate=round(adherence_rate, 4),
        prescription_day_count=_prescription_day_count(session, event),
        recent_side_effect_keep=has_recent_side_effect_keep(session, event),
    )


def create_clinician_escalation_stub(
    session: Session,
    event: DoseEvent,
    decision: AdherencePatternDecision,
) -> Notification | None:
    if not decision.escalation_stub_required:
        return None
    existing = _existing_clinician_escalation_stub(session, event.id, decision.pattern_code)
    if existing is not None:
        return existing
    visible_at = event.missed_detected_at or event.scheduled_for
    return create_notification(
        session,
        notification_type=CLINICIAN_ESCALATION_TYPE,
        title="의료진 확인 스텁",
        body="미복용 패턴을 의료진 확인 대상으로 기록했습니다.",
        visible_at=visible_at,
        related_dose_event_id=event.id,
        metadata={
            "category": CLINICIAN_ESCALATION_CATEGORY,
            "delivery_channel": "internal_only",
            "status": "stubbed",
            "dose_event_id": event.id,
            "pattern_code": decision.pattern_code,
            "pattern_label": decision.pattern_label,
            "reason": decision.reason,
            "streak_metrics": decision.streak_metrics.to_metadata(),
        },
    )


def validate_pattern_message(message: str, *, medication_name: str = "") -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not message.strip():
        errors.append("message_empty")
    forbidden_terms = set(FORBIDDEN_MESSAGE_TERMS)
    if medication_name.strip():
        forbidden_terms.add(medication_name.strip())
    for term in forbidden_terms:
        if term and term in message:
            errors.append(f"forbidden_term:{term}")
    if message.count("!") + message.count("?") > 1:
        errors.append("too_many_punctuation_marks")
    if len(message) > 45:
        errors.append("message_too_long")
    return not errors, errors


def _pattern_code(metrics: StreakMetrics) -> str:
    if metrics.recent_side_effect_keep:
        return "C"
    if metrics.current_consecutive_missed_days >= 3:
        return "D"
    if (
        metrics.overall_scheduled_count > 0
        and metrics.overall_adherence_rate < 0.5
        and metrics.current_consecutive_missed_days >= 2
        and metrics.prescription_day_count >= 15
    ):
        return "E"
    if 1 <= metrics.current_consecutive_missed_days <= 2 and metrics.previous_consecutive_taken_days >= 14:
        return "A"
    return "B"


def _pattern_reason(code: str, metrics: StreakMetrics) -> str:
    if code == "C":
        return "최근 7일 내 부작용 기록 후 알림 유지 선택이 확인되었습니다."
    if code == "D":
        return f"현재 시간대에서 {metrics.current_consecutive_missed_days}회 연속 미복용이 확인되었습니다."
    if code == "E":
        return (
            f"전체 복용률 {metrics.overall_adherence_rate:.0%}, "
            f"현재 시간대 {metrics.current_consecutive_missed_days}회 연속 미복용이 확인되었습니다."
        )
    if code == "A":
        return f"직전 {metrics.previous_consecutive_taken_days}회 연속 복용 후 단기 미복용이 확인되었습니다."
    return "복약 루틴 형성을 위한 단기 미복용 확인이 필요합니다."


def _slot_day_statuses(session: Session, event: DoseEvent) -> list[str]:
    end_of_day = datetime.combine(event.scheduled_for.date(), time.max)
    rows = session.scalars(
        select(DoseEvent)
        .where(
            DoseEvent.patient_id == event.patient_id,
            DoseEvent.slot_label == event.slot_label,
            DoseEvent.scheduled_for <= end_of_day,
        )
        .order_by(DoseEvent.scheduled_for.desc(), DoseEvent.id.desc())
    ).all()
    statuses_by_date: dict[Any, list[str]] = {}
    for row in rows:
        statuses_by_date.setdefault(row.scheduled_for.date(), []).append(row.status)
    return [_day_status(statuses_by_date[day]) for day in sorted(statuses_by_date, reverse=True)]


def _day_status(statuses: list[str]) -> str:
    if any(status == "missed" for status in statuses):
        return "missed"
    if statuses and all(status == "taken" for status in statuses):
        return "taken"
    return "scheduled"


def _streak_counts(day_statuses_desc: list[str]) -> tuple[int, int]:
    current_missed = 0
    index = 0
    while index < len(day_statuses_desc) and day_statuses_desc[index] == "missed":
        current_missed += 1
        index += 1
    previous_taken = 0
    while index < len(day_statuses_desc) and day_statuses_desc[index] == "taken":
        previous_taken += 1
        index += 1
    return current_missed, previous_taken


def _overall_counts(session: Session, event: DoseEvent) -> tuple[int, int]:
    rows = session.scalars(
        select(DoseEvent).where(
            DoseEvent.patient_id == event.patient_id,
            DoseEvent.scheduled_for <= event.scheduled_for,
            DoseEvent.status.in_(["taken", "missed"]),
        )
    ).all()
    taken = sum(1 for row in rows if row.status == "taken")
    missed = sum(1 for row in rows if row.status == "missed")
    return taken, missed


def _prescription_day_count(session: Session, event: DoseEvent) -> int:
    plan = session.get(MedicationPlan, event.plan_id)
    start_date = plan.start_date if plan is not None else event.scheduled_for.date()
    return max(1, (event.scheduled_for.date() - start_date).days + 1)


def has_recent_side_effect_keep(session: Session, event: DoseEvent, *, lookback_days: int = 7) -> bool:
    if is_reminder_suppressed_after_side_effect(session):
        return False
    reference_time = event.missed_detected_at or event.scheduled_for
    start_time = reference_time - timedelta(days=lookback_days)
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.patient_id == event.patient_id,
            Notification.notification_type == "conversation_alert",
            Notification.visible_at >= start_time,
            Notification.visible_at <= reference_time,
        )
        .order_by(Notification.visible_at.desc(), Notification.id.desc())
        .limit(100)
    ).all()
    for row in rows:
        metadata = parse_json_object(row.metadata_json)
        if metadata.get("category") != SIDE_EFFECT_REMINDER_SAFETY_CATEGORY:
            continue
        if metadata.get("status") == "reply_completed" and metadata.get("action") == "keep":
            return True
    return False


def _existing_clinician_escalation_stub(
    session: Session,
    dose_event_id: int,
    pattern_code: str,
) -> Notification | None:
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.related_dose_event_id == dose_event_id,
            Notification.notification_type == CLINICIAN_ESCALATION_TYPE,
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(20)
    ).all()
    for row in rows:
        metadata = parse_json_object(row.metadata_json)
        if metadata.get("category") == CLINICIAN_ESCALATION_CATEGORY and metadata.get("pattern_code") == pattern_code:
            return row
    return None
