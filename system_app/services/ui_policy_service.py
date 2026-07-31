from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import SystemPolicyOverride

MEDICATION_SCHEDULE_ALERT = "medication_schedule_alert"
MISSED_DOSE_CONVERSATION = "missed_dose_conversation"
UI_POLICY_KEYS = frozenset({MEDICATION_SCHEDULE_ALERT, MISSED_DOSE_CONVERSATION})
_STORAGE_PREFIX = "ui."


def ui_policy_enabled(
    session: Session,
    policy_key: str,
    *,
    patient_id: str | None = None,
) -> bool:
    if policy_key not in UI_POLICY_KEYS:
        raise ValueError("ui_policy_not_found")
    target_patient_id = patient_id or get_settings().patient_id
    row = session.scalar(
        select(SystemPolicyOverride)
        .where(
            SystemPolicyOverride.patient_id == target_patient_id,
            SystemPolicyOverride.policy_key == _storage_key(policy_key),
            SystemPolicyOverride.active.is_(True),
        )
        .order_by(desc(SystemPolicyOverride.updated_at), desc(SystemPolicyOverride.id))
    )
    if row is None:
        return True
    return str(row.value).strip().lower() == "true"


def ui_policy_state(
    session: Session,
    *,
    patient_id: str | None = None,
) -> dict[str, bool]:
    return {
        policy_key: ui_policy_enabled(session, policy_key, patient_id=patient_id)
        for policy_key in sorted(UI_POLICY_KEYS)
    }


def set_ui_policy(
    session: Session,
    policy_key: str,
    enabled: bool,
    *,
    patient_id: str | None = None,
) -> dict[str, bool]:
    if policy_key not in UI_POLICY_KEYS:
        raise ValueError("ui_policy_not_found")
    target_patient_id = patient_id or get_settings().patient_id
    storage_key = _storage_key(policy_key)
    rows = list(
        session.scalars(
            select(SystemPolicyOverride)
            .where(
                SystemPolicyOverride.patient_id == target_patient_id,
                SystemPolicyOverride.policy_key == storage_key,
            )
            .order_by(desc(SystemPolicyOverride.updated_at), desc(SystemPolicyOverride.id))
        ).all()
    )
    now = utc_now()
    if rows:
        row = rows[0]
        for stale in rows[1:]:
            stale.active = False
            stale.updated_at = now
        row.value = "true" if enabled else "false"
        row.reason = "Direct user setting from React UI."
        row.source = "patient_request"
        row.active = True
        row.updated_at = now
    else:
        session.add(
            SystemPolicyOverride(
                patient_id=target_patient_id,
                policy_key=storage_key,
                value="true" if enabled else "false",
                reason="Direct user setting from React UI.",
                source="patient_request",
                active=True,
                created_at=now,
                updated_at=now,
            )
        )
    session.flush()
    return ui_policy_state(session, patient_id=target_patient_id)


def _storage_key(policy_key: str) -> str:
    return f"{_STORAGE_PREFIX}{policy_key}"
