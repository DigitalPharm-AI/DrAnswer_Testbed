from __future__ import annotations

from dataclasses import dataclass

from shared.schemas import ResolvedNotificationPolicy, ResolvedPolicyBoundary


@dataclass(frozen=True)
class WorkbookPolicyRow:
    policy_key: str
    slot_label: str
    extra_reminders: int
    interval_minutes: int
    missed_dose_after_minutes: int
    primary_reminder_timing: str
    primary_reminder_offset_minutes: int
    medication_title_template: str
    medication_body_template: str
    extra_title_template: str
    extra_body_template: str
    missed_dose_title_template: str
    missed_dose_body_template: str
    active: bool
    priority: int

    def to_resolved_policy(self, *, requested_slot_label: str) -> ResolvedNotificationPolicy:
        return ResolvedNotificationPolicy(
            policy_key=self.policy_key,
            patient_id=None,
            slot_label=requested_slot_label,
            extra_reminders=self.extra_reminders,
            interval_minutes=self.interval_minutes,
            missed_dose_after_minutes=self.missed_dose_after_minutes,
            primary_reminder_timing=self.primary_reminder_timing,
            primary_reminder_offset_minutes=self.primary_reminder_offset_minutes,
            medication_title_template=self.medication_title_template,
            medication_body_template=self.medication_body_template,
            extra_title_template=self.extra_title_template,
            extra_body_template=self.extra_body_template,
            missed_dose_title_template=self.missed_dose_title_template,
            missed_dose_body_template=self.missed_dose_body_template,
            source="excel_default",
            reason=f"workbook:{self.slot_label}",
            priority=self.priority,
        )


@dataclass(frozen=True)
class WorkbookBoundaryRow:
    boundary_key: str
    policy_key: str
    min_extra_reminders: int
    max_extra_reminders: int
    min_interval_minutes: int
    max_interval_minutes: int
    min_missed_dose_after_minutes: int
    max_missed_dose_after_minutes: int
    allowed_primary_reminder_timings: tuple[str, ...]
    min_primary_reminder_offset_minutes: int
    max_primary_reminder_offset_minutes: int
    active: bool
    priority: int

    def to_resolved_boundary(self, *, requested_slot_label: str) -> ResolvedPolicyBoundary:
        return ResolvedPolicyBoundary(
            boundary_key=self.boundary_key,
            policy_key=self.policy_key,
            slot_label=requested_slot_label,
            min_extra_reminders=self.min_extra_reminders,
            max_extra_reminders=self.max_extra_reminders,
            min_interval_minutes=self.min_interval_minutes,
            max_interval_minutes=self.max_interval_minutes,
            min_missed_dose_after_minutes=self.min_missed_dose_after_minutes,
            max_missed_dose_after_minutes=self.max_missed_dose_after_minutes,
            allowed_primary_reminder_timings=list(self.allowed_primary_reminder_timings),
            min_primary_reminder_offset_minutes=self.min_primary_reminder_offset_minutes,
            max_primary_reminder_offset_minutes=self.max_primary_reminder_offset_minutes,
            reason=f"workbook:{self.policy_key}",
            priority=self.priority,
        )


@dataclass(frozen=True)
class WorkbookSystemPolicyRow:
    policy_key: str
    value: str
    active: bool
    priority: int
    description: str
