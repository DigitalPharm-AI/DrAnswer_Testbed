from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class PhrPatient(Base):
    __tablename__ = "phr_patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phr_patient_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PhrItemPrecaution(Base):
    __tablename__ = "phr_item_precautions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    precautions_text: Mapped[str] = mapped_column(Text, default="")
    severity_hint: Mapped[str] = mapped_column(String(40), default="moderate")
    keywords_json: Mapped[str] = mapped_column(Text, default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PhrPatientMedication(Base):
    __tablename__ = "phr_patient_medications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), default="")
    medication_name: Mapped[str] = mapped_column(String(255), default="")
    phr_patient_key: Mapped[str] = mapped_column(String(160), index=True, default="")
    item_name: Mapped[str] = mapped_column(String(255), index=True, default="")
    dosage: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PhrSideEffectAssessment(Base):
    __tablename__ = "phr_side_effect_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), default="")
    phr_patient_key: Mapped[str] = mapped_column(String(160), index=True, default="")
    medication_name: Mapped[str] = mapped_column(String(255), default="")
    symptom_text: Mapped[str] = mapped_column(Text)
    suspected: Mapped[bool] = mapped_column(Boolean, default=False)
    matched_effects_json: Mapped[str] = mapped_column(Text, default="[]")
    matched_items_json: Mapped[str] = mapped_column(Text, default="[]")
    matched_precautions_json: Mapped[str] = mapped_column(Text, default="[]")
    severity: Mapped[str] = mapped_column(String(40), default="none")
    evidence: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
