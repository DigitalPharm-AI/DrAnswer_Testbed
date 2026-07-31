from __future__ import annotations

import json
from datetime import UTC, date, datetime
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from agent_app.llm.messages import (
    ai_message_from_tool_calls,
    build_chat_messages,
    tool_messages_from_results,
)
from agent_app.tools.backend_query import BackendQueryTools
from agent_app.tools.medication_side_effects import (
    assess_side_effect_from_snapshot,
    side_effect_record_draft_from_snapshot,
)
from shared.chat_contracts import ChatSyncRequest, agent_chat_payload
from shared.schemas import ToolCallResult
from system_app.migrations import run_migrations
from system_app.models import (
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    NutritionFood,
    NutritionMeal,
    NutritionProfile,
    ReminderPolicy,
)
from tests.helpers import (
    build_backend_reader_url,
    build_system_engine,
)

NOW = datetime(2026, 7, 25, 10, 30, tzinfo=UTC)


def _database(_tmp_path=None):
    engine, _cleanup = build_system_engine("patient_snapshot")
    run_migrations(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    reader_url, _reader_cleanup = build_backend_reader_url(
        engine,
        "patient_snapshot_reader",
    )
    return reader_url, engine, sessions


def _seed_patient(
    session: Session,
    *,
    patient_id: str,
    medication_name: str,
    treatment_area: str,
) -> None:
    profile = NutritionProfile(
        patient_id=patient_id,
        name="테스트 환자",
        age=55,
        gender="female",
        disease="hypertension",
        activity_level="moderate",
    )
    plan = MedicationPlan(
        patient_id=patient_id,
        medication_name=medication_name,
        dosage="5mg",
        instructions="1정",
        treatment_area=treatment_area,
        source_type="test_scenario",
        source_key="snapshot-test",
        start_date=date(2026, 7, 1),
        end_date=date(2099, 12, 31),
        active=True,
    )
    session.add_all([profile, plan])
    session.flush()
    schedule = DoseSchedule(
        plan_id=plan.id,
        slot_label="아침",
        scheduled_time="08:00",
    )
    session.add(schedule)
    session.flush()
    session.add(
        DoseEvent(
            patient_id=patient_id,
            plan_id=plan.id,
            schedule_id=schedule.id,
            medication_name=medication_name,
            slot_label="아침",
            scheduled_for=NOW.replace(tzinfo=None, hour=8, minute=0),
            status="taken",
            taken_at=NOW.replace(tzinfo=None, hour=8, minute=5),
            version=2,
        )
    )
    meal = NutritionMeal(
        patient_id=patient_id,
        meal_type="breakfast",
        meal_date=NOW.date(),
        meal_time="08:20",
        description="아침 식사",
    )
    session.add(meal)
    session.flush()
    session.add(
        NutritionFood(
            meal_id=meal.id,
            food_name="현미밥",
            portion="1공기",
            calories=300,
        )
    )
    session.add(
        ReminderPolicy(
            patient_id=patient_id,
            policy_key="morning-dose",
            slot_label="아침",
            extra_reminders=1,
            interval_minutes=15,
            missed_dose_after_minutes=90,
            primary_reminder_timing="at",
            primary_reminder_offset_minutes=0,
            effective_start_date=date(2026, 7, 1),
            effective_end_date=date(2026, 12, 31),
            reason="snapshot test",
            source="test",
            active=True,
            version=3,
        )
    )


def test_snapshot_reads_required_current_domains_without_cross_patient_leak(
    tmp_path,
) -> None:
    reader_url, engine, sessions = _database(tmp_path)
    with sessions() as session:
        _seed_patient(
            session,
            patient_id="patient-a",
            medication_name="암로디핀 5mg",
            treatment_area="고혈압약",
        )
        _seed_patient(
            session,
            patient_id="patient-b",
            medication_name="메트포르민 500mg",
            treatment_area="당뇨약",
        )
        session.commit()

    queries = BackendQueryTools(reader_url)
    snapshot = queries.patient_context_snapshot(
        patient_id="patient-a",
        as_of=NOW,
    )

    assert snapshot["patient_id"] == "patient-a"
    assert snapshot["context_mode"] == "complete"
    assert snapshot["availability"] == {
        "profile": "available",
        "conditions_and_treatments": "available",
        "today_medication": "available",
        "today_meals": "available",
        "notification_policies": "available",
        "allergies": "not_supported",
        "clinical_observations": "not_supported",
    }
    assert snapshot["active_conditions"] == ["hypertension"]
    assert snapshot["active_treatments"] == ["고혈압약"]
    assert {
        item["medication_name"]
        for item in snapshot["active_medication_schedules"]
    } == {"암로디핀 5mg"}
    assert {
        item["medication_name"]
        for item in snapshot["today_medication"]["dose_events"]
    } == {"암로디핀 5mg"}
    assert [item["description"] for item in snapshot["today_meals"]] == [
        "아침 식사"
    ]
    assert [
        item["policy_key"] for item in snapshot["active_notification_policies"]
    ] == ["morning-dose"]
    assert "patient-b" not in json.dumps(snapshot, ensure_ascii=False)

    queries.engine.dispose()
    engine.dispose()


def test_snapshot_reads_all_current_domains_in_one_repeatable_read_transaction(
    tmp_path,
) -> None:
    reader_url, engine, sessions = _database(tmp_path)
    patient_id = "patient_0000000000000011"
    with sessions() as session:
        _seed_patient(
            session,
            patient_id=patient_id,
            medication_name="암로디핀 5mg",
            treatment_area="고혈압약",
        )
        session.commit()

    queries = BackendQueryTools(reader_url)
    domain_connection_ids: set[int] = set()
    transaction_statements: list[str] = []

    def observe_snapshot_sql(
        connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(str(statement).split())
        if "ai_v13_" in normalized:
            domain_connection_ids.add(id(connection))
        if normalized.upper().startswith(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        ):
            transaction_statements.append(normalized)

    event.listen(
        queries.engine,
        "before_cursor_execute",
        observe_snapshot_sql,
    )
    try:
        snapshot = queries.patient_context_snapshot(
            patient_id=patient_id,
            as_of=NOW,
        )
    finally:
        event.remove(
            queries.engine,
            "before_cursor_execute",
            observe_snapshot_sql,
        )

    assert snapshot["context_mode"] == "complete"
    assert len(domain_connection_ids) == 1
    assert transaction_statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
    ]

    queries.engine.dispose()
    engine.dispose()


def test_llm_payload_hides_trusted_identifiers_but_tool_payload_keeps_them(
    tmp_path,
) -> None:
    reader_url, engine, sessions = _database(tmp_path)
    with sessions() as session:
        _seed_patient(
            session,
            patient_id="patient_0000000000000012",
            medication_name="암로디핀 5mg",
            treatment_area="고혈압약",
        )
        session.commit()
    queries = BackendQueryTools(reader_url)
    snapshot = queries.patient_context_snapshot(
        patient_id="patient_0000000000000012",
        as_of=NOW,
    )
    request = ChatSyncRequest(
        request_id="req_0000000000000012",
        message_id="user_msg_0000000000000012",
        patient_id="patient_0000000000000012",
        requested_return_type="text",
        message="어제 이 약을 먹고 속이 메스꺼웠어",
        message_at=NOW,
    )
    payload = agent_chat_payload(
        request,
        backend_context={
            "patient_context_snapshot": snapshot,
            "recent_chat": [
                {
                    "message_id": "prior-message-secret",
                    "role": "user",
                    "message_type": "text",
                    "content": "어제 약을 먹고 속이 메스꺼웠어요",
                    "message": {
                        "text": "어제 약을 먹고 속이 메스꺼웠어요"
                    },
                    "created_at": NOW.isoformat(),
                }
            ],
            "structured_response_context": {
                "kind": "structured_chat_response",
                "source_message_id": "source-card-secret",
                "response_type": "selection_box",
                "response_value": "두부국",
                "source_message": {
                    "message_type": "selection_box",
                    "message": {
                        "selections": ["두부국", "소고기 두부국"]
                    },
                },
                "originating_user_message": {
                    "content": "두부된장국을 기록해 주세요."
                },
            },
            "mutation_resolution": {
                "confirmation_id": "confirmation-secret",
                "action_name": "create_medication_side_effect_record",
                "status": "applied",
                "tool_result": {
                    "record_id": "side-effect-secret",
                    "version": 3,
                    "message": "부작용 기록을 저장했습니다.",
                    "policy_id": "policy-public",
                },
            },
        },
    )

    assert (
        payload["context"]["trusted_patient_context"]["patient_id"]
        == "patient_0000000000000012"
    )
    trusted_event = payload["context"]["trusted_patient_context"][
        "today_medication"
    ]["dose_events"][0]
    assert trusted_event["dose_event_id"].startswith("dose_")
    assert trusted_event["version"] == 2
    payload["context"]["approved_user_action"] = {
        "status": "confirmed",
        "action_name": "create_nutrition_meal_record",
        "arguments": {"approval_key": "apv_private-capability"},
    }

    llm_message = build_chat_messages("system", payload)[1]
    llm_payload = json.loads(str(llm_message.content))
    serialized = json.dumps(llm_payload, ensure_ascii=False)
    assert "trusted_patient_context" not in serialized
    assert "patient_0000000000000012" not in serialized
    assert "dose_event_id" not in serialized
    assert '"version"' not in serialized
    assert "user_msg_0000000000000012" not in serialized
    assert "prior-message-secret" not in serialized
    assert "confirmation-secret" not in serialized
    assert "side-effect-secret" not in serialized
    assert "source-card-secret" not in serialized
    assert "apv_private-capability" not in serialized
    assert "approved_user_action" not in serialized
    assert llm_payload["context"]["recent_chat"][0]["content"] == (
        "어제 약을 먹고 속이 메스꺼웠어요"
    )
    assert (
        llm_payload["context"]["structured_response_context"][
            "response_value"
        ]
        == "두부국"
    )
    assert (
        llm_payload["context"]["structured_response_context"][
            "source_message"
        ]["message"]["selections"]
        == ["두부국", "소고기 두부국"]
    )
    assert "mutation_resolution" not in llm_payload["context"]

    queries.engine.dispose()
    engine.dispose()


def test_tool_messages_hide_technical_ids_but_keep_model_relevant_results() -> None:
    ai_message = ai_message_from_tool_calls(
        [
            {
                "id": "protocol-tool-call",
                "name": "get_side_effect_history",
                "arguments": {},
            }
        ]
    )
    result = ToolCallResult(
        tool_name="get_side_effect_history",
        status="success",
        response={
            "patient_id": "patient-secret",
            "record_id": "record-secret",
            "version": 4,
            "medication_name": "암로디핀 5mg",
            "symptom_text": "메스꺼움",
            "policy_id": "policy-public",
            "food_ref_id": "food-reference",
            "nested": {
                "id": 991,
                "related_dose_event_id": "dose-secret",
                "message": "기록을 확인했습니다.",
            },
        },
        idempotency_key="idempotency-secret",
    )

    [tool_message] = tool_messages_from_results(
        ai_message,
        [
            {
                "id": "protocol-tool-call",
                "name": "get_side_effect_history",
                "arguments": {},
            }
        ],
        [result],
    )
    projected = json.loads(str(tool_message.content))
    serialized = json.dumps(projected, ensure_ascii=False)

    assert tool_message.tool_call_id == "protocol-tool-call"
    assert "patient-secret" not in serialized
    assert "record-secret" not in serialized
    assert "dose-secret" not in serialized
    assert "food-reference" not in serialized
    assert "idempotency-secret" not in serialized
    assert '"version"' not in serialized
    assert '"id"' not in serialized
    assert projected["response"]["medication_name"] == "암로디핀 5mg"
    assert projected["response"]["symptom_text"] == "메스꺼움"
    assert projected["response"]["policy_id"] == "policy-public"
    assert projected["response"]["nested"]["message"] == "기록을 확인했습니다."


def test_read_tool_messages_keep_only_contract_public_target_ids() -> None:
    ai_message = ai_message_from_tool_calls(
        [
            {
                "id": "meal-list-call",
                "name": "get_nutrition_meal_record_list",
                "arguments": {},
            }
        ]
    )
    result = ToolCallResult(
        tool_name="get_nutrition_meal_record_list",
        status="success",
        response={
            "patient_id": "patient-secret",
            "meals": [
                {
                    "id": 100,
                    "meal_id": "meal_public",
                    "version": 3,
                    "foods": [
                        {
                            "id": 200,
                            "food_id": "food_public",
                            "food_ref_id": "food_reference_public",
                        }
                    ],
                }
            ],
        },
        idempotency_key="idempotency-secret",
    )

    [tool_message] = tool_messages_from_results(
        ai_message,
        [
            {
                "id": "meal-list-call",
                "name": "get_nutrition_meal_record_list",
                "arguments": {},
            }
        ],
        [result],
    )
    projected = json.loads(str(tool_message.content))
    serialized = json.dumps(projected, ensure_ascii=False)

    assert "patient-secret" not in serialized
    assert "idempotency-secret" not in serialized
    assert '"version"' not in serialized
    assert '"id"' not in serialized
    meal = projected["response"]["meals"][0]
    assert meal["meal_id"] == "meal_public"
    assert meal["foods"][0]["food_id"] == "food_public"
    assert meal["foods"][0]["food_ref_id"] == "food_reference_public"


def test_side_effect_tool_keeps_all_matches_without_forcing_attribution() -> None:
    snapshot = {
        "as_of": NOW.isoformat(),
        "availability": {"today_medication": "available"},
        "active_medication_schedules": [
            {
                "medication_name": "메트포르민 500mg",
                "treatment_area": "당뇨약",
            },
            {
                "medication_name": "수니티닙 25mg",
                "treatment_area": "신장암 치료약",
            },
        ],
        "today_medication": {
            "dose_events": [
                {
                    "dose_event_id": "dose_metformin",
                    "medication_name": "메트포르민 500mg",
                    "scheduled_for": NOW.isoformat(),
                    "version": 4,
                }
            ]
        },
    }
    result = assess_side_effect_from_snapshot(
        symptom_text="속이 메스꺼웠어",
        patient_snapshot=snapshot,
    )
    assert result.suspected is True
    assert result.matched_items == [
        "메트포르민 500mg",
        "수니티닙 25mg",
    ]

    unscoped_draft = side_effect_record_draft_from_snapshot(
        symptom_text="속이 메스꺼웠어",
        symptom_onset_text="어제 복용 후",
        medication_name=None,
        patient_snapshot=snapshot,
        trace_id="trace-side-effect-all-matches",
        source_event_type="medication_agent",
    )
    assert unscoped_draft["medication_name"] is None
    assert unscoped_draft["matched_items"] == [
        "메트포르민 500mg",
        "수니티닙 25mg",
    ]
    assert unscoped_draft["related_dose_event_id"] is None

    draft = side_effect_record_draft_from_snapshot(
        symptom_text="속이 메스꺼웠어",
        symptom_onset_text="어제 복용 후",
        medication_name="메트포르민",
        patient_snapshot=snapshot,
        trace_id="trace-side-effect",
        source_event_type="medication_agent",
    )
    assert draft["medication_name"] == "메트포르민"
    assert draft["related_dose_event_id"] == "dose_metformin"
    assert draft["symptom_onset_text"] == "어제 복용 후"
    assert "patient_id" not in draft
    assert "version" not in draft
