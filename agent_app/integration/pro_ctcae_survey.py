from __future__ import annotations

import hmac
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.state_crypto import (
    AgentStateCipher,
    AgentStateCipherError,
)
from agent_app.persistence.models import (
    AgentProCtcaeResponse,
    AgentProCtcaeSurvey,
)
from shared.backend_v13_contracts import (
    ProCtcaeQuestion,
    ProCtcaeResponse,
    ProCtcaeSeverityResult,
)
from shared.json_utils import canonical_json
from shared.schemas import AEProCtcaeAssessmentResult, AgentResponse
from shared.settings import Settings
from shared.time_utils import utc_now
from shared.tool_names import GET_MEDICATION_SIDE_EFFECT_ASSESSMENT

AWAITING_RESPONSE = "AWAITING_RESPONSE"
COMPLETED = "COMPLETED"
APPROVAL_PENDING = "APPROVAL_PENDING"
APPLIED = "APPLIED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
SUPERSEDED = "SUPERSEDED"
ACTIVE_STATUSES = (
    AWAITING_RESPONSE,
    COMPLETED,
    APPROVAL_PENDING,
)


class ProCtcaeSurveyError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ProCtcaeSurveyEncryptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProCtcaeSurveyEncryptionContext:
    survey_id: str
    patient_id_hash: str

    def associated_data(self) -> bytes:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class ProCtcaeSurveyCipher:
    """Encrypt patient-scoped questionnaire state stored in the Agent DB."""

    def __init__(self, *, key_id: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(
                "pro_ctcae_survey_encryption_key_must_be_32_bytes"
            )
        if not key_id.strip():
            raise ValueError(
                "pro_ctcae_survey_encryption_key_id_required"
            )
        self.key_id = key_id.strip()
        self._state_cipher = AgentStateCipher(key)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
    ) -> ProCtcaeSurveyCipher:
        # The Agent already requires this 32-byte at-rest encryption key.
        # Questionnaire state uses a separate AAD and HMAC purpose namespace.
        key_id, key = settings.require_agent_feedback_encryption()
        return cls(key_id=key_id, key=key)

    def patient_digest(self, patient_id: str) -> str:
        return self._digest(
            b"pro-ctcae-patient-id-v1\0"
            + patient_id.encode("utf-8")
        )

    def payload_digest(self, payload: dict[str, Any]) -> str:
        return self._digest(
            b"pro-ctcae-survey-payload-v1\0"
            + canonical_json(payload).encode("utf-8")
        )

    def encrypt_payload(
        self,
        payload: dict[str, Any],
        *,
        context: ProCtcaeSurveyEncryptionContext,
    ) -> str:
        return self._state_cipher.encrypt(
            canonical_json(payload).encode("utf-8"),
            associated_data=context.associated_data(),
        )

    def decrypt_payload(
        self,
        token: str,
        *,
        context: ProCtcaeSurveyEncryptionContext,
        expected_hash: str,
    ) -> dict[str, Any]:
        try:
            plaintext = self._state_cipher.decrypt(
                token,
                associated_data=context.associated_data(),
                version_error=(
                    "unsupported_pro_ctcae_survey_ciphertext_version"
                ),
                authentication_error=(
                    "pro_ctcae_survey_ciphertext_authentication_failed"
                ),
            ).decode("utf-8")
            value = json.loads(plaintext)
            if not isinstance(value, dict):
                raise ValueError("survey_payload_not_object")
            if not hmac.compare_digest(
                self.payload_digest(value),
                expected_hash,
            ):
                raise ValueError("survey_payload_hash_mismatch")
            return value
        except AgentStateCipherError as exc:
            raise ProCtcaeSurveyEncryptionError(str(exc)) from exc
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ProCtcaeSurveyEncryptionError(
                "pro_ctcae_survey_ciphertext_authentication_failed"
            ) from exc

    def _digest(self, value: bytes) -> str:
        return self._state_cipher.digest(value)


@dataclass(frozen=True)
class ProCtcaeSurveyTransition:
    kind: Literal["next_question", "completed"]
    survey_id: str
    symptom_name: str
    question: ProCtcaeQuestion | None
    question_number: int
    question_count: int
    severity: ProCtcaeSeverityResult | None
    completed_context: dict[str, Any] | None
    symptom_number: int = 1
    symptom_count: int = 1


class ProCtcaeSurveyService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        cipher: ProCtcaeSurveyCipher | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.cipher = cipher or ProCtcaeSurveyCipher.from_settings(
            settings
        )
        self.response_ttl_seconds = int(
            settings.agent_pro_ctcae_survey_ttl_seconds
        )
        self.retention_seconds = int(
            settings.agent_pro_ctcae_survey_retention_seconds
        )

    def start_from_agent_response(
        self,
        *,
        patient_id: str,
        origin_message_id: str,
        trace_id: str,
        user_message: str,
        response: AgentResponse,
    ) -> ProCtcaeSurveyTransition | None:
        questionnaires = _questionnaires_from_response(response)
        if not questionnaires:
            return None
        structured = response.structured_payload
        lookup_response = _side_effect_lookup_response(structured)
        assessments = _side_effect_assessments(lookup_response)
        tool_arguments = _side_effect_tool_arguments(structured)
        medication_name = str(tool_arguments.get("medication_name") or "").strip()
        symptom_states: list[dict[str, Any]] = []
        questions: list[ProCtcaeQuestion] = []
        seen_concepts: set[tuple[str, str]] = set()
        for questionnaire in questionnaires:
            contract_questions = _contract_questions(questionnaire)
            if not contract_questions:
                raise ProCtcaeSurveyError(
                    "pro_ctcae_survey_questions_missing",
                )
            concept_key = (
                str(questionnaire.matched_symptom_term or "").strip(),
                str(
                    questionnaire.matched_korean_symptom_name or ""
                ).strip(),
            )
            if concept_key != ("", "") and concept_key in seen_concepts:
                continue
            seen_concepts.add(concept_key)
            symptom_text = str(
                questionnaire.input_symptom or user_message
            ).strip()
            assessment = _assessment_for_symptom(
                assessments,
                symptom_text,
            )
            symptom_name = str(
                questionnaire.matched_korean_symptom_name
                or questionnaire.questions[0].korean_symptom_name
                or "증상"
            ).strip()
            symptom_state = {
                "symptom_name": symptom_name,
                "symptom_text": symptom_text,
                "symptom_onset_text": str(
                    assessment.get("symptom_onset_text") or ""
                ).strip(),
                "medication_name": medication_name or None,
                "matched_items": _string_list(
                    assessment.get("matched_items")
                ),
                "matched_effects": _string_list(
                    assessment.get("matched_effects")
                ),
                "suspected": bool(assessment.get("suspected")),
                "questions": [
                    question.model_dump(mode="json")
                    for question in contract_questions
                ],
                "severity": None,
            }
            symptom_states.append(symptom_state)
            questions.extend(contract_questions)
        if not symptom_states:
            return None
        item_codes = [question.item_code for question in questions]
        if len(set(item_codes)) != len(item_codes):
            raise ProCtcaeSurveyError(
                "pro_ctcae_survey_batch_question_codes_duplicate",
            )
        state = {
            "symptoms": symptom_states,
            "questions": [
                question.model_dump(mode="json")
                for question in questions
            ],
            "approval_cursor": 0,
            "approval_outcomes": [],
        }
        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()

        with self.session_factory() as session:
            existing = session.scalar(
                select(AgentProCtcaeSurvey)
                .where(
                    AgentProCtcaeSurvey.origin_message_id
                    == origin_message_id
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.patient_id_hash != patient_hash:
                    raise ProCtcaeSurveyError(
                        "pro_ctcae_survey_origin_message_reused",
                    )
                transition = self._transition_for_row(
                    session,
                    existing,
                )
                session.commit()
                return transition

            session.execute(
                update(AgentProCtcaeSurvey)
                .where(
                    AgentProCtcaeSurvey.patient_id_hash
                    == patient_hash,
                    AgentProCtcaeSurvey.status.in_(ACTIVE_STATUSES),
                )
                .values(
                    status=SUPERSEDED,
                    resolved_at=now,
                    updated_at=now,
                )
            )
            survey_id = f"survey_{uuid4().hex}"
            context = ProCtcaeSurveyEncryptionContext(
                survey_id=survey_id,
                patient_id_hash=patient_hash,
            )
            row = AgentProCtcaeSurvey(
                public_id=survey_id,
                patient_id_hash=patient_hash,
                origin_message_id=origin_message_id,
                expected_origin_message_id=origin_message_id,
                trace_id=trace_id,
                status=AWAITING_RESPONSE,
                question_count=len(questions),
                current_question_index=0,
                payload_ciphertext=self.cipher.encrypt_payload(
                    state,
                    context=context,
                ),
                payload_hash=self.cipher.payload_digest(state),
                encryption_key_id=self.cipher.key_id,
                version=1,
                created_at=now,
                updated_at=now,
                response_expires_at=now
                + timedelta(seconds=self.response_ttl_seconds),
                expires_at=now
                + timedelta(seconds=self.retention_seconds),
            )
            session.add(row)
            session.flush()
            transition = self._transition_for_row(session, row)
            session.commit()
            return transition

    def submit_response(
        self,
        *,
        patient_id: str,
        current_user_message_id: str,
        originating_user_message_id: str,
        submitted_value: str,
    ) -> ProCtcaeSurveyTransition | None:
        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()
        normalized_value = submitted_value.strip()
        if not normalized_value:
            raise ProCtcaeSurveyError(
                "pro_ctcae_survey_response_invalid",
            )

        with self.session_factory() as session:
            duplicate = session.execute(
                select(
                    AgentProCtcaeSurvey,
                    AgentProCtcaeResponse,
                )
                .join(
                    AgentProCtcaeResponse,
                    AgentProCtcaeResponse.survey_id
                    == AgentProCtcaeSurvey.id,
                )
                .where(
                    AgentProCtcaeSurvey.patient_id_hash
                    == patient_hash,
                    AgentProCtcaeResponse.source_user_message_id
                    == current_user_message_id,
                )
                .with_for_update()
            ).first()
            if duplicate is not None:
                survey, response = duplicate
                state = self._state(survey)
                questions = _questions_from_state(state)
                question = _question_by_code(
                    questions,
                    response.item_code,
                )
                if (
                    response.response_index
                    >= len(question.response_options)
                    or question.response_options[
                        response.response_index
                    ]
                    != normalized_value
                ):
                    raise ProCtcaeSurveyError(
                        "pro_ctcae_survey_idempotency_conflict",
                    )
                transition = self._transition_for_row(
                    session,
                    survey,
                )
                session.commit()
                return transition

            survey = session.scalar(
                select(AgentProCtcaeSurvey)
                .where(
                    AgentProCtcaeSurvey.patient_id_hash
                    == patient_hash,
                    AgentProCtcaeSurvey.status
                    == AWAITING_RESPONSE,
                )
                .with_for_update()
            )
            if survey is None:
                return None
            if survey.response_expires_at <= now:
                survey.status = EXPIRED
                survey.resolved_at = now
                survey.updated_at = now
                session.commit()
                raise ProCtcaeSurveyError(
                    "pro_ctcae_survey_expired",
                )
            if (
                survey.expected_origin_message_id
                != originating_user_message_id
            ):
                raise ProCtcaeSurveyError(
                    "pro_ctcae_survey_stale_response",
                )

            state = self._state(survey)
            questions = _questions_from_state(state)
            responses = self._responses(session, survey.id)
            remaining = _unanswered_questions(
                questions,
                responses,
            )
            if not remaining:
                transition = self._complete(
                    survey,
                    state=state,
                    questions=questions,
                    responses=responses,
                    now=now,
                )
                session.commit()
                return transition

            current_question = remaining[0]
            try:
                response_index = (
                    current_question.response_options.index(
                        normalized_value
                    )
                )
            except ValueError as exc:
                raise ProCtcaeSurveyError(
                    "pro_ctcae_survey_response_invalid",
                ) from exc

            session.add(
                AgentProCtcaeResponse(
                    survey_id=survey.id,
                    item_code=current_question.item_code,
                    response_index=response_index,
                    source_user_message_id=current_user_message_id,
                    answered_at=now,
                )
            )
            session.flush()
            responses = self._responses(session, survey.id)
            remaining = _unanswered_questions(
                questions,
                responses,
            )
            if remaining:
                next_question = remaining[0]
                next_index = questions.index(next_question)
                survey.current_question_index = next_index
                survey.expected_origin_message_id = (
                    current_user_message_id
                )
                survey.response_expires_at = now + timedelta(
                    seconds=self.response_ttl_seconds
                )
                survey.updated_at = now
                survey.version += 1
                transition = _question_transition(
                    survey,
                    state=state,
                    questions=questions,
                    question=next_question,
                )
                session.commit()
                return transition

            transition = self._complete(
                survey,
                state=state,
                questions=questions,
                responses=responses,
                now=now,
            )
            session.commit()
            return transition

    def mark_approval_pending(
        self,
        *,
        patient_id: str,
        survey_id: str,
    ) -> None:
        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()
        with self.session_factory() as session:
            changed = session.execute(
                update(AgentProCtcaeSurvey)
                .where(
                    AgentProCtcaeSurvey.public_id == survey_id,
                    AgentProCtcaeSurvey.patient_id_hash
                    == patient_hash,
                    AgentProCtcaeSurvey.status == COMPLETED,
                )
                .values(
                    status=APPROVAL_PENDING,
                    updated_at=now,
                    version=AgentProCtcaeSurvey.version + 1,
                )
            ).rowcount
            session.commit()
            if changed not in (0, 1):
                raise ProCtcaeSurveyError(
                    "pro_ctcae_survey_state_update_failed",
                    retryable=True,
                )

    def resolve_approval(
        self,
        *,
        patient_id: str,
        applied: bool,
    ) -> ProCtcaeSurveyTransition | None:
        """Resolve one record approval and return the next batch item."""

        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()
        with self.session_factory() as session:
            survey = session.scalar(
                select(AgentProCtcaeSurvey)
                .where(
                    AgentProCtcaeSurvey.patient_id_hash
                    == patient_hash,
                    AgentProCtcaeSurvey.status == APPROVAL_PENDING,
                )
                .with_for_update()
            )
            if survey is None:
                session.commit()
                return None
            state = self._state(survey)
            questions = _questions_from_state(state)
            responses = self._responses(session, survey.id)
            contexts = _completed_contexts(
                survey,
                state=state,
                questions=questions,
                responses=responses,
            )
            cursor = max(0, int(state.get("approval_cursor") or 0))
            outcomes = state.get("approval_outcomes")
            if not isinstance(outcomes, list):
                outcomes = []
            outcomes.append(
                {
                    "symptom_number": cursor + 1,
                    "applied": applied,
                }
            )
            next_cursor = cursor + 1
            state["approval_cursor"] = next_cursor
            state["approval_outcomes"] = outcomes
            survey.updated_at = now
            survey.version += 1
            if next_cursor < len(contexts):
                survey.status = COMPLETED
                context = ProCtcaeSurveyEncryptionContext(
                    survey_id=survey.public_id,
                    patient_id_hash=survey.patient_id_hash,
                )
                survey.payload_ciphertext = self.cipher.encrypt_payload(
                    state,
                    context=context,
                )
                survey.payload_hash = self.cipher.payload_digest(state)
                transition = _completed_transition(
                    survey,
                    state=state,
                    questions=questions,
                    responses=responses,
                )
                session.commit()
                return transition
            survey.status = (
                APPLIED
                if any(
                    isinstance(outcome, dict)
                    and outcome.get("applied") is True
                    for outcome in outcomes
                )
                else CANCELLED
            )
            survey.resolved_at = now
            context = ProCtcaeSurveyEncryptionContext(
                survey_id=survey.public_id,
                patient_id_hash=survey.patient_id_hash,
            )
            survey.payload_ciphertext = self.cipher.encrypt_payload(
                state,
                context=context,
            )
            survey.payload_hash = self.cipher.payload_digest(state)
            session.commit()
            return None

    def _transition_for_row(
        self,
        session: Session,
        survey: AgentProCtcaeSurvey,
    ) -> ProCtcaeSurveyTransition:
        state = self._state(survey)
        questions = _questions_from_state(state)
        responses = self._responses(session, survey.id)
        remaining = _unanswered_questions(
            questions,
            responses,
        )
        if remaining:
            return _question_transition(
                survey,
                state=state,
                questions=questions,
                question=remaining[0],
            )
        return _completed_transition(
            survey,
            state=state,
            questions=questions,
            responses=responses,
        )

    def _complete(
        self,
        survey: AgentProCtcaeSurvey,
        *,
        state: dict[str, Any],
        questions: list[ProCtcaeQuestion],
        responses: list[AgentProCtcaeResponse],
        now: datetime,
    ) -> ProCtcaeSurveyTransition:
        symptoms = _symptom_states(state)
        if symptoms:
            for symptom in symptoms:
                symptom_questions = _questions_for_symptom(symptom)
                severity = _build_severity(
                    symptom_questions,
                    responses,
                )
                symptom["severity"] = severity.model_dump(mode="json")
            state["symptoms"] = symptoms
        else:
            severity = _build_severity(
                questions,
                responses,
            )
            state["severity"] = severity.model_dump(mode="json")
        context = ProCtcaeSurveyEncryptionContext(
            survey_id=survey.public_id,
            patient_id_hash=survey.patient_id_hash,
        )
        survey.payload_ciphertext = self.cipher.encrypt_payload(
            state,
            context=context,
        )
        survey.payload_hash = self.cipher.payload_digest(state)
        survey.status = COMPLETED
        survey.current_question_index = len(questions)
        survey.completed_at = now
        survey.updated_at = now
        survey.version += 1
        return _completed_transition(
            survey,
            state=state,
            questions=questions,
            responses=responses,
        )

    def _state(
        self,
        survey: AgentProCtcaeSurvey,
    ) -> dict[str, Any]:
        if survey.encryption_key_id != self.cipher.key_id:
            raise ProCtcaeSurveyEncryptionError(
                "pro_ctcae_survey_encryption_key_unavailable"
            )
        return self.cipher.decrypt_payload(
            survey.payload_ciphertext,
            context=ProCtcaeSurveyEncryptionContext(
                survey_id=survey.public_id,
                patient_id_hash=survey.patient_id_hash,
            ),
            expected_hash=survey.payload_hash,
        )

    @staticmethod
    def _responses(
        session: Session,
        survey_id: int,
    ) -> list[AgentProCtcaeResponse]:
        return list(
            session.scalars(
                select(AgentProCtcaeResponse)
                .where(
                    AgentProCtcaeResponse.survey_id
                    == survey_id
                )
                .order_by(AgentProCtcaeResponse.id)
            ).all()
        )


def pro_ctcae_question_response(
    transition: ProCtcaeSurveyTransition,
    *,
    trace_id: str,
) -> AgentResponse:
    question = transition.question
    if transition.kind != "next_question" or question is None:
        raise ValueError("pro_ctcae_next_question_required")
    first_question = transition.question_number == 1
    guidance = (
        f"{transition.symptom_name} 증상이 약물과 관련이 있을 수 있습니다. "
        "아래의 질문에 답변해 주시면 증상을 더 정확하게 평가할 수 있습니다."
        if first_question
        else "이어서 다음 질문에 답변해 주세요."
    )
    text = f"{guidance}\n\n{question.question}"
    return AgentResponse(
        trace_id=trace_id,
        agent_name="pro_ctcae_survey_state",
        prompt_version_id="pro_ctcae_survey_v1",
        decision_type="pro_ctcae_questionnaire",
        structured_payload={
            "routing_mode": "pro_ctcae_survey_state",
            "executed_by": "pro_ctcae_survey_state",
            "final_answer_source": "deterministic_survey_state",
            "survey_progress": {
                "current": transition.question_number,
                "total": transition.question_count,
                "symptom_current": transition.symptom_number,
                "symptom_total": transition.symptom_count,
            },
            "chat_response": {
                "message_type": "selection_box",
                "message": {
                    "message_title": (
                        f"{transition.symptom_name} 관련 자가 보고 설문 "
                        f"({transition.question_number}/"
                        f"{transition.question_count})"
                        + (
                            f" · 증상 {transition.symptom_number}/"
                            f"{transition.symptom_count}"
                            if transition.symptom_count > 1
                            else ""
                        )
                    ),
                    "text": text,
                    "tables": None,
                    "selections": list(
                        question.response_options
                    ),
                    "inputs": None,
                },
            },
        },
        human_summary=text,
        requires_conversation_alert=False,
    )


def _questionnaires_from_response(
    response: AgentResponse,
) -> list[AEProCtcaeAssessmentResult]:
    structured = response.structured_payload
    raw_items = structured.get("ae_pro_ctcae_items")
    if isinstance(raw_items, list):
        raw_questionnaires = raw_items
    else:
        raw = structured.get("ae_pro_ctcae")
        raw_questionnaires = [raw] if isinstance(raw, dict) else []
    if not raw_questionnaires:
        return []
    try:
        return [
            AEProCtcaeAssessmentResult.model_validate(raw)
            for raw in raw_questionnaires
            if isinstance(raw, dict)
        ]
    except Exception as exc:
        raise ProCtcaeSurveyError(
            "pro_ctcae_survey_questionnaire_invalid",
        ) from exc


def _side_effect_lookup_response(
    structured: dict[str, Any],
) -> dict[str, Any]:
    lookup = structured.get("side_effect_lookup")
    if not isinstance(lookup, dict):
        return {}
    response = lookup.get("response")
    return response if isinstance(response, dict) else {}


def _side_effect_assessments(
    lookup_response: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = lookup_response.get("assessments")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return [lookup_response] if lookup_response else []


def _assessment_for_symptom(
    assessments: list[dict[str, Any]],
    symptom_text: str,
) -> dict[str, Any]:
    normalized = symptom_text.strip().casefold()
    for assessment in assessments:
        if (
            str(assessment.get("symptom_text") or "")
            .strip()
            .casefold()
            == normalized
        ):
            return assessment
    return assessments[0] if len(assessments) == 1 else {}


def _contract_questions(
    questionnaire: AEProCtcaeAssessmentResult,
) -> list[ProCtcaeQuestion]:
    questions = [
        ProCtcaeQuestion(
            item_code=question.item_code,
            question=question.question,
            response_type=(
                question.response_type.strip()
                or "single_choice"
            ),
            response_options=question.response_options,
        )
        for question in questionnaire.questions
    ]
    item_codes = [question.item_code for question in questions]
    if len(set(item_codes)) != len(item_codes):
        raise ProCtcaeSurveyError(
            "pro_ctcae_survey_question_codes_duplicate",
        )
    return questions


def _side_effect_tool_arguments(
    structured: dict[str, Any],
) -> dict[str, Any]:
    calls = structured.get("tool_calls")
    if not isinstance(calls, list):
        return {}
    for call in calls:
        if not isinstance(call, dict):
            continue
        if str(call.get("name") or "") != (
            GET_MEDICATION_SIDE_EFFECT_ASSESSMENT
        ):
            continue
        arguments = call.get("arguments")
        return arguments if isinstance(arguments, dict) else {}
    return {}


def _questions_from_state(
    state: dict[str, Any],
) -> list[ProCtcaeQuestion]:
    raw = state.get("questions")
    if not isinstance(raw, list):
        raise ProCtcaeSurveyError(
            "pro_ctcae_survey_questions_missing",
            retryable=True,
        )
    try:
        return [
            ProCtcaeQuestion.model_validate(question)
            for question in raw
        ]
    except Exception as exc:
        raise ProCtcaeSurveyError(
            "pro_ctcae_survey_questions_invalid",
            retryable=True,
        ) from exc


def _unanswered_questions(
    questions: list[ProCtcaeQuestion],
    responses: list[AgentProCtcaeResponse],
) -> list[ProCtcaeQuestion]:
    answered_item_codes = {
        response.item_code
        for response in responses
    }
    return [
        question
        for question in questions
        if question.item_code not in answered_item_codes
    ]


def _build_severity(
    questions: list[ProCtcaeQuestion],
    responses: list[AgentProCtcaeResponse],
) -> ProCtcaeSeverityResult:
    response_by_code = {
        response.item_code: response
        for response in responses
    }
    values: list[ProCtcaeResponse] = []
    for question in questions:
        row = response_by_code.get(question.item_code)
        if row is None:
            raise ProCtcaeSurveyError(
                "pro_ctcae_survey_response_missing",
            )
        if row.response_index >= len(question.response_options):
            raise ProCtcaeSurveyError(
                "pro_ctcae_survey_response_index_invalid",
            )
        values.append(
            ProCtcaeResponse(
                item_code=question.item_code,
                response_index=row.response_index,
                response_text=question.response_options[
                    row.response_index
                ],
            )
        )
    return ProCtcaeSeverityResult(
        questions=questions,
        responses=values,
    )


def _severity_from_state_or_responses(
    state: dict[str, Any],
    *,
    questions: list[ProCtcaeQuestion],
    responses: list[AgentProCtcaeResponse],
) -> ProCtcaeSeverityResult:
    raw = state.get("severity")
    if isinstance(raw, dict):
        return ProCtcaeSeverityResult.model_validate(raw)
    return _build_severity(questions, responses)


def _symptom_states(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("symptoms")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _questions_for_symptom(
    symptom: dict[str, Any],
) -> list[ProCtcaeQuestion]:
    raw = symptom.get("questions")
    if not isinstance(raw, list):
        raise ProCtcaeSurveyError(
            "pro_ctcae_survey_questions_missing",
            retryable=True,
        )
    try:
        return [
            ProCtcaeQuestion.model_validate(question)
            for question in raw
        ]
    except Exception as exc:
        raise ProCtcaeSurveyError(
            "pro_ctcae_survey_questions_invalid",
            retryable=True,
        ) from exc


def _symptom_for_question(
    symptoms: list[dict[str, Any]],
    question: ProCtcaeQuestion,
) -> tuple[int, dict[str, Any] | None]:
    for index, symptom in enumerate(symptoms):
        if any(
            candidate.item_code == question.item_code
            for candidate in _questions_for_symptom(symptom)
        ):
            return index, symptom
    return 0, None


def _completed_contexts(
    survey: AgentProCtcaeSurvey,
    *,
    state: dict[str, Any],
    questions: list[ProCtcaeQuestion],
    responses: list[AgentProCtcaeResponse],
) -> list[dict[str, Any]]:
    symptoms = _symptom_states(state)
    if not symptoms:
        severity = _severity_from_state_or_responses(
            state,
            questions=questions,
            responses=responses,
        )
        symptoms = [state]
        severities = [severity]
    else:
        severities = []
        for symptom in symptoms:
            symptom_questions = _questions_for_symptom(symptom)
            raw_severity = symptom.get("severity")
            severity = (
                ProCtcaeSeverityResult.model_validate(raw_severity)
                if isinstance(raw_severity, dict)
                else _build_severity(symptom_questions, responses)
            )
            severities.append(severity)
    contexts: list[dict[str, Any]] = []
    for index, (symptom, severity) in enumerate(
        zip(symptoms, severities, strict=True)
    ):
        contexts.append(
            {
                "survey_id": survey.public_id,
                "symptom_number": index + 1,
                "symptom_count": len(symptoms),
                "symptom_name": str(
                    symptom.get("symptom_name") or "증상"
                ),
                "symptom_text": str(
                    symptom.get("symptom_text") or ""
                ),
                "symptom_onset_text": str(
                    symptom.get("symptom_onset_text") or ""
                ),
                "medication_name": symptom.get("medication_name"),
                "suspected": bool(symptom.get("suspected")),
                "matched_items": _string_list(
                    symptom.get("matched_items")
                ),
                "matched_effects": _string_list(
                    symptom.get("matched_effects")
                ),
                "severity": severity.model_dump(mode="json"),
            }
        )
    return contexts


def _question_transition(
    survey: AgentProCtcaeSurvey,
    *,
    state: dict[str, Any],
    questions: list[ProCtcaeQuestion],
    question: ProCtcaeQuestion,
) -> ProCtcaeSurveyTransition:
    symptoms = _symptom_states(state)
    symptom_index, symptom = _symptom_for_question(
        symptoms,
        question,
    )
    symptom_questions = (
        _questions_for_symptom(symptom)
        if symptom is not None
        else questions
    )
    symptom_question_index = symptom_questions.index(question)
    return ProCtcaeSurveyTransition(
        kind="next_question",
        survey_id=survey.public_id,
        symptom_name=str(
            (symptom or state).get("symptom_name") or "증상"
        ),
        question=question,
        question_number=symptom_question_index + 1,
        question_count=len(symptom_questions),
        severity=None,
        completed_context=None,
        symptom_number=symptom_index + 1,
        symptom_count=max(1, len(symptoms)),
    )


def _completed_transition(
    survey: AgentProCtcaeSurvey,
    *,
    state: dict[str, Any],
    questions: list[ProCtcaeQuestion],
    responses: list[AgentProCtcaeResponse],
) -> ProCtcaeSurveyTransition:
    completed_contexts = _completed_contexts(
        survey,
        state=state,
        questions=questions,
        responses=responses,
    )
    if not completed_contexts:
        raise ProCtcaeSurveyError(
            "pro_ctcae_survey_completion_missing",
            retryable=True,
        )
    cursor = min(
        max(0, int(state.get("approval_cursor") or 0)),
        len(completed_contexts) - 1,
    )
    completed_context = completed_contexts[cursor]
    severity = ProCtcaeSeverityResult.model_validate(
        completed_context["severity"]
    )
    return ProCtcaeSurveyTransition(
        kind="completed",
        survey_id=survey.public_id,
        symptom_name=str(completed_context.get("symptom_name") or "증상"),
        question=None,
        question_number=len(questions),
        question_count=len(questions),
        severity=severity,
        completed_context=completed_context,
        symptom_number=cursor + 1,
        symptom_count=len(completed_contexts),
    )


def _question_by_code(
    questions: list[ProCtcaeQuestion],
    item_code: str,
) -> ProCtcaeQuestion:
    for question in questions:
        if question.item_code == item_code:
            return question
    raise ProCtcaeSurveyError(
        "pro_ctcae_survey_response_question_missing",
        retryable=True,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if str(item).strip()
        )
    )
