from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import SystemPolicyOverride

settings = get_settings()

SIDE_EFFECT_REMINDER_SAFETY_CATEGORY = "side_effect_reminder_safety"
SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY = "side_effect_reminder_suppressed"
def is_reminder_suppressed_after_side_effect(session: Session) -> bool:
    override = session.scalar(
        select(SystemPolicyOverride)
        .where(
            SystemPolicyOverride.patient_id == settings.patient_id,
            SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY,
            SystemPolicyOverride.active.is_(True),
        )
        .order_by(desc(SystemPolicyOverride.updated_at), desc(SystemPolicyOverride.created_at), desc(SystemPolicyOverride.id))
    )
    if override is None:
        return False
    return override.value.strip().lower() == "true"


def set_reminder_suppressed_after_side_effect(
    session: Session,
    suppressed: bool,
    *,
    reason: str = "PRO-CTCAE 부작용 기록 후 환자 알림 안전 확인 응답",
) -> None:
    active_rows = session.scalars(
        select(SystemPolicyOverride).where(
            SystemPolicyOverride.patient_id == settings.patient_id,
            SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY,
            SystemPolicyOverride.active.is_(True),
        )
    ).all()
    for row in active_rows:
        row.active = False
        row.updated_at = utc_now()
    session.add(
        SystemPolicyOverride(
            patient_id=settings.patient_id,
            policy_key=SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY,
            value="true" if suppressed else "false",
            reason=reason,
            source="patient_request",
            active=True,
        )
    )
    session.flush()
