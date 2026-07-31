from pathlib import Path

from shared.schemas import SideEffectRecordRequest, SideEffectRecordView
from shared.settings import Settings
from system_app.models import SideEffectRecord


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_standalone_phr_service_files_remain_absent() -> None:
    assert not (REPOSITORY_ROOT / "phr_app").exists()
    assert not (REPOSITORY_ROOT / ".env.phr_app.example").exists()
    assert not (REPOSITORY_ROOT / "system_app/services/phr_client.py").exists()


def test_runtime_settings_exclude_standalone_phr_connections() -> None:
    fields = Settings.model_fields
    assert "phr_database_url" not in fields
    assert "phr_base_url" not in fields
    assert "phr_read_only" not in fields
    assert "phr_trace_logging" not in fields


def test_side_effect_contract_and_model_exclude_phr_patient_key() -> None:
    assert "phr_patient_key" not in SideEffectRecordRequest.model_fields
    assert "phr_patient_key" not in SideEffectRecordView.model_fields
    assert "phr_patient_key" not in SideEffectRecord.__table__.columns
