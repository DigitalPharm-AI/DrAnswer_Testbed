from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.settings import get_settings
from system_app.models import MedicationPlan
from system_app.services.clock_service import ensure_clock

settings = get_settings()


def ensure_base_data(session: Session) -> None:
    ensure_clock(session)
    session.flush()


def active_medication_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count(MedicationPlan.id)).where(
                MedicationPlan.patient_id == settings.patient_id,
                MedicationPlan.active.is_(True),
            )
        )
        or 0
    )


def simulation_readiness(session: Session) -> dict:
    active_count = active_medication_count(session)
    if active_count == 0:
        return {
            "ready": False,
            "reason": "medication_required",
            "message": "복약 정보를 먼저 입력해주세요.",
        }
    return {
        "ready": True,
        "reason": "active_medication_plan_ready",
        "message": "활성 복약 일정이 등록되어 시뮬레이션을 실행할 수 있습니다.",
    }


def can_run_simulation(session: Session) -> bool:
    return bool(simulation_readiness(session)["ready"])
