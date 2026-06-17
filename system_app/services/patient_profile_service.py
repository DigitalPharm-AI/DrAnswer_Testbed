from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from shared.schemas import PhrMedicationRegistrationItem, PhrPatientRegistrationResult
from shared.settings import get_settings
from system_app.models import ChatMessage, MedicationPlan, SimulationPatientProfile
from system_app.services.clock_service import ensure_clock
from system_app.services.simulation_constants import PHR_SYNC_FAILED, PHR_SYNC_NEEDS_SYNC, PHR_SYNC_SYNCED, PHR_SYNC_UNREGISTERED

settings = get_settings()
REMOVED_WELCOME_MESSAGE = "복약 정보를 입력하면 PHR 등록을 완료한 뒤 시뮬레이션을 시작할 수 있어요. 시간 진행과 배속 재생으로 시나리오를 빠르게 검증해보세요."


def ensure_base_data(session: Session) -> None:
    ensure_clock(session)
    session.execute(delete(ChatMessage).where(ChatMessage.content == REMOVED_WELCOME_MESSAGE))
    session.flush()

def get_patient_profile(session: Session) -> SimulationPatientProfile | None:
    return session.scalar(select(SimulationPatientProfile).where(SimulationPatientProfile.local_patient_id == settings.patient_id))

def ensure_patient_profile(session: Session) -> SimulationPatientProfile:
    profile = get_patient_profile(session)
    if profile is not None:
        return profile
    profile = SimulationPatientProfile(
        local_patient_id=settings.patient_id,
        phr_patient_key="",
        sync_status=PHR_SYNC_UNREGISTERED,
        error_message="",
        updated_at=datetime.utcnow(),
    )
    session.add(profile)
    session.flush()
    return profile

def get_phr_patient_key(session: Session) -> str | None:
    profile = get_patient_profile(session)
    if profile is None or not profile.phr_patient_key:
        return None
    return profile.phr_patient_key

def mark_phr_sync_needed(session: Session) -> None:
    profile = ensure_patient_profile(session)
    profile.sync_status = PHR_SYNC_NEEDS_SYNC if profile.phr_patient_key else PHR_SYNC_UNREGISTERED
    profile.error_message = ""
    profile.updated_at = datetime.utcnow()
    clock = ensure_clock(session)
    clock.is_running = False
    clock.speed_multiplier = 0
    clock.last_tick_real_at = datetime.utcnow()
    session.flush()

def build_phr_registration_items(session: Session) -> list[PhrMedicationRegistrationItem]:
    rows = session.scalars(select(MedicationPlan).where(MedicationPlan.active.is_(True)).order_by(MedicationPlan.medication_name.asc())).all()
    return [PhrMedicationRegistrationItem(item_name=row.medication_name, dosage=row.dosage or "") for row in rows]

def apply_phr_registration_result(session: Session, result: PhrPatientRegistrationResult) -> SimulationPatientProfile:
    profile = ensure_patient_profile(session)
    now = datetime.utcnow()
    profile.phr_patient_key = result.phr_patient_key
    profile.sync_status = PHR_SYNC_SYNCED
    profile.error_message = ""
    if profile.registered_at is None:
        profile.registered_at = now
    profile.updated_at = now
    session.flush()
    return profile

def mark_phr_sync_failed(session: Session, error_message: str) -> SimulationPatientProfile:
    profile = ensure_patient_profile(session)
    profile.sync_status = PHR_SYNC_FAILED
    profile.error_message = error_message[:1000]
    profile.updated_at = datetime.utcnow()
    session.flush()
    return profile

def phr_profile_view(session: Session) -> dict:
    profile = get_patient_profile(session)
    if profile is None:
        return {
            "local_patient_id": settings.patient_id,
            "phr_patient_key": "",
            "sync_status": PHR_SYNC_UNREGISTERED,
            "status_label": "PHR 등록 필요",
            "detail": "설정 완료 후 PHR에 복약 정보를 등록해야 부작용 조회가 가능합니다.",
            "registered_at": None,
            "error_message": "",
        }
    if profile.sync_status == PHR_SYNC_SYNCED:
        status_label = "PHR key 발급 완료"
        detail = f"PHR key: {profile.phr_patient_key[:12]}..."
    elif profile.sync_status == PHR_SYNC_NEEDS_SYNC:
        status_label = "PHR 재동기화 필요"
        detail = "복약 계획이 변경되어 PHR 복약 원장 동기화가 필요합니다."
    elif profile.sync_status == PHR_SYNC_FAILED:
        status_label = "PHR 등록/동기화 실패"
        detail = profile.error_message or "PHR 서버 응답을 확인해주세요."
    else:
        status_label = "PHR 등록 필요"
        detail = "설정 완료 후 PHR에 복약 정보를 등록해야 부작용 조회가 가능합니다."
    return {
        "local_patient_id": profile.local_patient_id,
        "phr_patient_key": profile.phr_patient_key,
        "sync_status": profile.sync_status,
        "status_label": status_label,
        "detail": detail,
        "registered_at": profile.registered_at,
        "error_message": profile.error_message,
    }


def active_medication_count(session: Session) -> int:
    return session.scalar(select(func.count(MedicationPlan.id)).where(MedicationPlan.active.is_(True))) or 0


def simulation_readiness(session: Session) -> dict:
    active_count = active_medication_count(session)
    if active_count == 0:
        return {
            "ready": False,
            "reason": "medication_required",
            "message": "복약 정보를 먼저 입력해주세요.",
        }
    profile = get_patient_profile(session)
    if profile is None or profile.sync_status != PHR_SYNC_SYNCED:
        return {
            "ready": False,
            "reason": "phr_sync_required",
            "message": "복약 정보를 PHR로 등록한 뒤 시뮬레이션을 실행할 수 있습니다.",
        }
    return {
        "ready": True,
        "reason": "ready",
        "message": "PHR 등록이 완료되어 시뮬레이션을 실행할 수 있습니다.",
    }


def can_run_simulation(session: Session) -> bool:
    return bool(simulation_readiness(session)["ready"])
