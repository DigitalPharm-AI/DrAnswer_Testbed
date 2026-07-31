from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from agent_app.integration.approval_state import CONSUMED
from shared.backend_v13_contracts import (
    NotificationPolicyChangeResponse,
    NotificationPolicyChangeResult,
    RecordChangeResponse,
    RecordChangeResult,
)
from agent_app.llm.messages import langchain_tools_from_catalog
from agent_app.persistence.db import SessionLocal
from agent_app.persistence.models import (
    AgentBackendWriteRequest,
    AgentPendingAction,
)
from agent_app.tools.backend_write import BACKEND_WRITE_TOOL_SPECS
from shared.tool_catalog import ToolCatalog
from agent_app.tools.mcp_server import AgentMcpToolServer, _approval_display
from shared.tool_names import (
    BACKEND_V13_SYNC_WRITE_TOOLS,
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    GET_NOTIFICATION_POLICIES,
    MEDICATION_CHAT_TOOLS,
    NUTRITION_MANAGEMENT_TOOLS,
    RECORD_APPROVAL_ACTIONS,
    REQUEST_RECORD_APPROVAL,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from agent_app.tools.protocol import tool_result_from_mcp_result
from shared.schemas import ToolCallResult

NOW = datetime(2026, 7, 25, 16, 30, tzinfo=timezone.utc)
PATIENT_ID = "patient_0000000000000001"
SOURCE_CHAT_REQUEST_ID = "req_0000000000000001"
SOURCE_MESSAGE_ID = "user_msg_0000000000000001"
BEDROCK_UNSUPPORTED_SCHEMA_COMBINATORS = {
    "oneOf",
    "allOf",
    "anyOf",
}


def test_meal_approval_display_uses_record_arguments() -> None:
    display = _approval_display(
        CREATE_NUTRITION_MEAL_RECORD,
        {
            "meal_type": "breakfast",
            "foods": [
                {
                    "food_name": "비빔밥",
                    "portion": "450g",
                    "nutrients": {"calories": 600},
                },
                {
                    "food_name": "미역국",
                    "nutrients": {"calories": 80},
                },
            ],
        },
        display_context={
            "requested_at": "2026-04-20T09:30:00+09:00",
        },
    )

    assert display == {
        "title": "식사 기록",
        "question": "다음 식사 내용을 기록할까요?",
        "tables": [
            {
                "table_title": None,
                "rows": [
                    {"column": "시기", "value": "아침"},
                    {
                        "column": "음식 종류",
                        "value": "비빔밥, 미역국",
                    },
                    {
                        "column": "기록 요청 시간",
                        "value": "2026-04-20 09:30",
                    },
                    {
                        "column": "기록 대상 시간",
                        "value": "2026-04-20 아침",
                    },
                ],
            }
        ],
        "action_label": "기록",
    }


def test_side_effect_approval_display_uses_completed_survey_data() -> None:
    display = _approval_display(
        CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        {
            "symptom_text": (
                "속이 메스꺼운데 어제 먹은 약때문일까?"
            ),
            "symptom_onset_text": "어제 복용 후",
            "suspected": True,
            "matched_items": [
                "메트포르민 500mg",
                "수니티닙 50mg",
            ],
            "matched_effects": [
                "메트포르민 500mg: 메스꺼움",
                "수니티닙 50mg: 메스꺼움",
            ],
            "severity": {
                "questions": [
                    {
                        "item_code": "PROCTCAE_NAUSEA_FREQ",
                        "question": (
                            "지난 일주일 동안, 메스꺼움을 "
                            "얼마나 자주 느꼈습니까?"
                        ),
                        "response_type": "빈도(Frequency)",
                        "response_options": [
                            "전혀 없다",
                            "자주 있다",
                        ],
                    },
                    {
                        "item_code": "PROCTCAE_NAUSEA_SEV",
                        "question": (
                            "지난 일주일 동안, 메스꺼움이 "
                            "가장 심할 때는 어느 정도였습니까?"
                        ),
                        "response_type": "정도(Severity)",
                        "response_options": [
                            "전혀 없다",
                            "심하다",
                        ],
                    },
                ],
                "responses": [
                    {
                        "item_code": "PROCTCAE_NAUSEA_FREQ",
                        "response_index": 1,
                        "response_text": "자주 있다",
                    },
                    {
                        "item_code": "PROCTCAE_NAUSEA_SEV",
                        "response_index": 1,
                        "response_text": "심하다",
                    },
                ],
            },
        },
        display_context={
            "symptom_name": "메스꺼움",
            "requested_at": "2026-04-20T09:30:00+09:00",
        },
    )

    assert display == {
        "title": "부작용 평가 기록",
        "question": "다음 부작용 평가 결과를 기록할까요?",
        "tables": [
            {
                "table_title": None,
                "rows": [
                    {"column": "증상", "value": "메스꺼움"},
                    {
                        "column": "빈도(Frequency)",
                        "value": "자주 있다",
                    },
                    {
                        "column": "정도(Severity)",
                        "value": "심하다",
                    },
                    {
                        "column": "관련 가능 약물",
                        "value": (
                            "메트포르민 500mg, 수니티닙 50mg"
                        ),
                    },
                    {
                        "column": "기록 요청 시간",
                        "value": "2026-04-20 09:30",
                    },
                    {
                        "column": "기록 대상 시간",
                        "value": "어제 복용 후",
                    },
                ],
            }
        ],
        "action_label": "기록",
    }


@pytest.mark.parametrize(
    "response_type",
    [
        "양/정도(Amount)",
        "정도+해당사항 없음",
        "정도+성생활/무응답 선택지",
        "자유기입(Free text)",
    ],
)
def test_side_effect_approval_display_preserves_excel_response_type(
    response_type: str,
) -> None:
    display = _approval_display(
        CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        {
            "symptom_text": "증상 원문",
            "symptom_onset_text": "오늘",
            "severity": {
                "questions": [
                    {
                        "item_code": "a",
                        "question": "Excel 질문 원문",
                        "response_type": response_type,
                        "response_options": ["Excel 응답 원문"],
                    }
                ],
                "responses": [
                    {
                        "item_code": "a",
                        "response_index": 0,
                        "response_text": "Excel 응답 원문",
                    }
                ],
            },
        },
        display_context={
            "symptom_name": "증상",
            "requested_at": "2026-04-20T09:30:00+09:00",
        },
    )

    rows = display["tables"][0]["rows"]
    assert {
        "column": response_type,
        "value": "Excel 응답 원문",
    } in rows


def test_dose_approval_display_uses_scheduled_time_from_snapshot() -> None:
    dose_event_id = "dose_event_0000000000000001"
    display = _approval_display(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        {"dose_event_id": dose_event_id},
        display_context={
            "requested_at": "2026-04-20T09:35:00+09:00",
            "trusted_patient_context": {
                "today_medication": {
                    "dose_events": [
                        {
                            "dose_event_id": dose_event_id,
                            "scheduled_for": (
                                "2026-04-20T08:00:00+09:00"
                            ),
                        }
                    ]
                }
            },
        },
    )

    assert display["tables"] == [
        {
            "table_title": None,
            "rows": [
                {
                    "column": "기록 요청 시간",
                    "value": "2026-04-20 09:35",
                },
                {
                    "column": "기록 대상 시간",
                    "value": "2026-04-20 08:00",
                },
            ],
        }
    ]


@pytest.mark.parametrize(
    ("action_name", "arguments"),
    [
        (
            CREATE_MEDICATION_SIDE_EFFECT_RECORD,
            {
                "symptom_text": "속이 메스꺼워요",
                "symptom_onset_text": "오늘 아침",
            },
        ),
        (
            UPDATE_MEDICATION_DOSE_EVENT_STATUS,
            {"dose_event_id": "dose-event-1"},
        ),
        (
            CREATE_NUTRITION_MEAL_RECORD,
            {
                "meal_type": "breakfast",
                "foods": [{"food_name": "토스트"}],
            },
        ),
        (
            UPDATE_NUTRITION_MEAL_RECORD,
            {"meal_id": "meal-1", "description": "수정"},
        ),
        (
            DELETE_NUTRITION_MEAL_RECORD,
            {"meal_id": "meal-1"},
        ),
        (
            UPDATE_NUTRITION_FOOD_RECORD,
            {
                "meal_id": "meal-1",
                "food_id": "food-1",
                "food_name": "통밀 토스트",
            },
        ),
        (
            DELETE_NUTRITION_FOOD_RECORD,
            {"meal_id": "meal-1", "food_id": "food-1"},
        ),
        (
            CHANGE_NOTIFICATION_POLICY,
            {
                "policy_id": "policy-1",
                "decision": "apply",
                "changes": {
                    "effective_start_date": "2026-04-21",
                },
            },
        ),
        (
            UPSERT_NUTRITION_PREFERENCE_FACT,
            {
                "predicate": "likes",
                "object_label": "토스트",
            },
        ),
    ],
)
def test_every_approval_display_has_request_and_target_time(
    action_name: str,
    arguments: dict,
) -> None:
    display = _approval_display(
        action_name,
        arguments,
        display_context={
            "requested_at": "2026-04-20T09:30:00+09:00",
            "trusted_patient_context": {
                "today_medication": {
                    "dose_events": [
                        {
                            "dose_event_id": "dose-event-1",
                            "scheduled_for": (
                                "2026-04-20T08:00:00+09:00"
                            ),
                        }
                    ]
                },
                "today_meals": [
                    {
                        "id": "meal-1",
                        "meal_type": "breakfast",
                        "meal_date": "2026-04-20",
                        "meal_time": "08:15:00",
                    }
                ],
            },
        },
    )

    rows = [
        row
        for table in display["tables"]
        for row in table["rows"]
    ]
    time_values = {
        row["column"]: row["value"]
        for row in rows
        if row["column"] in {
            "기록 요청 시간",
            "기록 대상 시간",
        }
    }
    assert time_values["기록 요청 시간"] == "2026-04-20 09:30"
    assert time_values["기록 대상 시간"] != "확인되지 않음"


def _schema_property_union(
    tools: dict[str, dict],
    action_names: set[str] | frozenset[str],
) -> set[str]:
    return {
        property_name
        for action_name in action_names
        for property_name in tools[action_name]["inputSchema"][
            "properties"
        ]
    }


def _assert_bedrock_tool_schema_has_no_combinators(
    value: object,
) -> None:
    if isinstance(value, dict):
        assert not BEDROCK_UNSUPPORTED_SCHEMA_COMBINATORS.intersection(
            value
        )
        for child in value.values():
            _assert_bedrock_tool_schema_has_no_combinators(child)
    elif isinstance(value, list):
        for child in value:
            _assert_bedrock_tool_schema_has_no_combinators(child)


@pytest.fixture(autouse=True)
def clear_backend_write_state() -> None:
    with SessionLocal() as session:
        session.execute(delete(AgentBackendWriteRequest))
        session.execute(delete(AgentPendingAction))
        session.commit()
    yield
    with SessionLocal() as session:
        session.execute(delete(AgentBackendWriteRequest))
        session.execute(delete(AgentPendingAction))
        session.commit()


class FakeBackendV13Client:
    def __init__(self) -> None:
        self.record_requests = []
        self.policy_requests = []

    async def change_record(self, request):
        self.record_requests.append(request)
        return RecordChangeResponse(
            request_id=request.request_id,
            result=RecordChangeResult(
                resource_type=request.resource_type,
                operation=request.operation,
                record_id=request.record_id or "meal_0000000000000101",
                parent_record_id=request.parent_record_id,
                version=1 if request.operation == "create" else (request.expected_version or 0) + 1,
            ),
            processed_at=NOW,
        )

    async def change_notification_policy(self, request):
        self.policy_requests.append(request)
        return NotificationPolicyChangeResponse(
            request_id=request.request_id,
            result=NotificationPolicyChangeResult(
                policy_id=request.policy_id,
                decision=request.payload.decision,
                version=request.expected_version + (1 if request.payload.decision == "apply" else 0),
            ),
            processed_at=NOW,
        )


class FakeBackendQueryTools:
    def __init__(self) -> None:
        self.version_queries = []

    def record_version(
        self,
        *,
        patient_id,
        resource_type,
        record_id,
        parent_record_id=None,
    ):
        self.version_queries.append(
            {
                "patient_id": patient_id,
                "resource_type": resource_type,
                "record_id": record_id,
                "parent_record_id": parent_record_id,
            }
        )
        return 7

    def notification_policy_version(self, *, patient_id, policy_id):
        self.version_queries.append(
            {
                "patient_id": patient_id,
                "resource_type": "notification_policy",
                "record_id": policy_id,
            }
        )
        return 4

    def notification_policies(self, *, patient_id, policy_id=None, slot_label=None, active_only=True):
        return {
            "success": True,
            "policies": [
                {
                    "policy_id": policy_id or "npol_0000000000000001",
                    "slot_label": slot_label or "아침",
                    "version": 4,
                }
            ],
            "total": 1,
            "source": "backend_read_db",
        }


def v13_payload() -> dict:
    return {
        "patient_id": PATIENT_ID,
        "current_time": NOW,
        "context": {
            "request_metadata": {
                "contract_version": "v1.3",
                "request_id": SOURCE_CHAT_REQUEST_ID,
                "message_id": SOURCE_MESSAGE_ID,
            }
        },
    }


def seed_internal_approval(
    server: AgentMcpToolServer,
    payload: dict,
    *,
    tool_call_id: str,
) -> str:
    request_metadata = payload["context"]["request_metadata"]
    action_name = str(payload.pop("_approved_action_name"))
    arguments = dict(payload.pop("_approved_arguments"))
    action_label = (
        "변경"
        if action_name == CHANGE_NOTIFICATION_POLICY
        else "수정"
    )
    display = {
        "title": "승인 테스트",
        "question": "확인한 내용을 적용할까요?",
        "action_label": action_label,
    }
    server.backend_writes.approval_store.prepare(
        patient_id=payload["patient_id"],
        source_chat_request_id="req_0000000000000090",
        source_message_id="user_msg_0000000000000090",
        trace_id="internal-trace-not-sent",
        action_name=action_name,
        tool_call_id=tool_call_id,
        arguments=arguments,
        display=display,
    )
    decision = server.backend_writes.approval_store.submit_response(
        patient_id=payload["patient_id"],
        current_user_message_id=request_metadata["message_id"],
        current_source_chat_request_id=request_metadata["request_id"],
        originating_user_message_id="user_msg_0000000000000090",
        submitted_value=action_label,
        source_message={
            "message_type": "selection_box",
            "message": {
                "message_title": display["title"],
                "text": display["question"],
                "selections": [action_label, "취소"],
            },
        },
    )
    assert decision is not None and decision.kind == "approved"
    payload["context"]["approved_user_action"] = {
        "status": "confirmed",
        "action_name": action_name,
        "arguments": {"approval_key": decision.approval_key},
    }
    return decision.approval_key


async def execute(
    server: AgentMcpToolServer,
    *,
    name: str,
    arguments: dict,
    source_event_type: str,
    tool_call_id: str,
    payload: dict | None = None,
):
    result = await server.tools_call(
        {
            "name": name,
            "arguments": arguments,
            "tool_call_id": tool_call_id,
        },
        trace_id="internal-trace-not-sent",
        source_event_type=source_event_type,
        payload=payload or v13_payload(),
    )
    return tool_result_from_mcp_result(name, result)


def test_backend_v13_write_registry_has_only_contract_supported_tools() -> None:
    assert frozenset(BACKEND_WRITE_TOOL_SPECS) == BACKEND_V13_SYNC_WRITE_TOOLS
    assert len(BACKEND_WRITE_TOOL_SPECS) == 8
    side_effect = BACKEND_WRITE_TOOL_SPECS[
        CREATE_MEDICATION_SIDE_EFFECT_RECORD
    ]
    assert side_effect.endpoint == "record_change"
    assert side_effect.resource_type == "medication_side_effect"
    assert side_effect.operation == "create"
    assert side_effect.requires_expected_version is False


def test_write_tool_catalog_exposes_only_business_arguments() -> None:
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}

    create_tool = tools[CREATE_NUTRITION_MEAL_RECORD]
    update_tool = tools[UPDATE_NUTRITION_MEAL_RECORD]
    policy_tool = tools[CHANGE_NOTIFICATION_POLICY]
    approval_tool = tools[REQUEST_RECORD_APPROVAL]

    assert create_tool["execution_mode"] == "backend_sync"
    assert create_tool["backend_endpoint"] == "/agent/sync/record-change"
    assert "expected_version" not in create_tool["required_arguments"]
    assert "expected_version" not in update_tool["required_arguments"]
    assert "patient_id" not in update_tool["optional_arguments"]
    assert "reason" not in update_tool["optional_arguments"]
    assert update_tool["inputSchema"]["additionalProperties"] is False
    assert policy_tool["backend_endpoint"] == "/agent/sync/notification-policy-change"
    assert policy_tool["annotations"]["idempotentHint"] is True
    assert policy_tool["required_arguments"] == ["policy_id", "decision"]
    assert policy_tool["inputSchema"]["additionalProperties"] is False
    assert policy_tool["inputSchema"]["properties"]["changes"]["additionalProperties"] is False
    approval_schema = approval_tool["inputSchema"]
    record_arguments_schema = approval_schema["properties"][
        "record_arguments"
    ]
    assert set(
        approval_schema["properties"]["action_name"]["enum"]
    ) == RECORD_APPROVAL_ACTIONS
    assert record_arguments_schema["additionalProperties"] is False
    assert record_arguments_schema["required"] == []
    assert set(record_arguments_schema["properties"]) == (
        _schema_property_union(tools, RECORD_APPROVAL_ACTIONS)
    )
    _assert_bedrock_tool_schema_has_no_combinators(approval_schema)


@pytest.mark.parametrize(
    "specialist_tools",
    [MEDICATION_CHAT_TOOLS, NUTRITION_MANAGEMENT_TOOLS],
)
def test_model_write_catalog_exposes_only_scoped_approval_wrapper(
    specialist_tools,
) -> None:
    expected_actions = set(specialist_tools).intersection(
        RECORD_APPROVAL_ACTIONS
    )
    model_tools = {
        tool["name"]: tool
        for tool in ToolCatalog.model_tools_for(*specialist_tools)
    }

    assert REQUEST_RECORD_APPROVAL in model_tools
    assert not expected_actions.intersection(model_tools)
    approval_schema = model_tools[REQUEST_RECORD_APPROVAL]["inputSchema"]
    assert set(
        approval_schema["properties"]["action_name"]["enum"]
    ) == expected_actions
    record_arguments_schema = approval_schema["properties"][
        "record_arguments"
    ]
    all_tools = {
        tool["name"]: tool
        for tool in ToolCatalog.available_tools_payload()
    }
    assert record_arguments_schema["additionalProperties"] is False
    assert record_arguments_schema["required"] == []
    assert set(record_arguments_schema["properties"]) == (
        _schema_property_union(all_tools, expected_actions)
    )
    _assert_bedrock_tool_schema_has_no_combinators(approval_schema)

    bound_tools = langchain_tools_from_catalog(
        list(model_tools.values())
    )
    bound_approval_schema = next(
        tool["function"]["parameters"]
        for tool in bound_tools
        if tool["function"]["name"] == REQUEST_RECORD_APPROVAL
    )
    _assert_bedrock_tool_schema_has_no_combinators(
        bound_approval_schema
    )

    # Trusted server-forced execution still resolves the raw canonical Tools.
    server_tools = {
        tool["name"] for tool in ToolCatalog.tools_for(*specialist_tools)
    }
    assert expected_actions <= server_tools


def test_model_catalog_never_exposes_raw_record_write_tools() -> None:
    assert (
        ToolCatalog.model_tools_for(*RECORD_APPROVAL_ACTIONS)
        == []
    )


@pytest.mark.asyncio
async def test_v13_record_write_tool_requires_record_approval() -> None:
    client = FakeBackendV13Client()
    server = AgentMcpToolServer(
        backend_client=client,
        backend_queries=FakeBackendQueryTools(),
    )
    arguments = {
        "meal_type": "lunch",
        "meal_date": "2026-07-25",
        "foods": [
            {
                "food_name": "닭가슴살",
                "portion": "100g",
                "nutrients": {"calories": 165},
            }
        ],
    }

    first = await execute(
        server,
        name=CREATE_NUTRITION_MEAL_RECORD,
        arguments=arguments,
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-001",
    )
    assert first.status == "error"
    assert first.error == "record_approval_required"
    assert first.response["required_tool"] == REQUEST_RECORD_APPROVAL
    assert client.record_requests == []


@pytest.mark.asyncio
async def test_record_write_never_falls_back_when_contract_metadata_is_missing() -> None:
    client = FakeBackendV13Client()
    server = AgentMcpToolServer(
        backend_client=client,
        backend_queries=FakeBackendQueryTools(),
    )

    result = await execute(
        server,
        name=CREATE_NUTRITION_MEAL_RECORD,
        arguments={
            "meal_type": "lunch",
            "meal_date": "2026-07-25",
            "foods": [{"food_name": "현미밥", "portion": "1공기"}],
        },
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-missing-contract-metadata",
        payload={
            "patient_id": PATIENT_ID,
            "current_time": NOW,
            "context": {},
        },
    )

    assert result.status == "error"
    assert result.error == "record_approval_required"
    assert result.response == {
        "contract_version": "v1.3",
        "required_tool": REQUEST_RECORD_APPROVAL,
    }
    assert client.record_requests == []


@pytest.mark.asyncio
async def test_request_record_approval_prepares_bound_target_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = AgentMcpToolServer(
        backend_client=FakeBackendV13Client(),
        backend_queries=FakeBackendQueryTools(),
    )
    captured = {}

    async def prepare(tool_name, arguments, **kwargs):
        captured.update(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "result_tool_name": kwargs["result_tool_name"],
            }
        )
        return ToolCallResult(
            tool_name=REQUEST_RECORD_APPROVAL,
            status="confirmation_required",
            response={
                "mutation_confirmation": {
                    "action_name": tool_name,
                    "display": {
                        "title": "식사 기록",
                        "action_label": "기록",
                    },
                }
            },
            idempotency_key="approval-request-001",
        )

    monkeypatch.setattr(server, "_prepare_internal_approval", prepare)
    record_arguments = {
        "meal_type": "breakfast",
        "confidence": 0.91,
        "evidence_text": "다른 approval action에만 허용되는 필드",
        "foods": [
            {
                "food_name": "짜장면",
                "nutrients": {"calories": 800},
                "unexpected_nested_field": "remove-me",
            }
        ],
    }
    expected_record_arguments = {
        "meal_type": "breakfast",
        "foods": [
            {
                "food_name": "짜장면",
                "nutrients": {"calories": 800},
            }
        ],
    }

    result = await execute(
        server,
        name=REQUEST_RECORD_APPROVAL,
        arguments={
            "action_name": CREATE_NUTRITION_MEAL_RECORD,
            "record_arguments": record_arguments,
        },
        source_event_type="nutrition_management_agent",
        tool_call_id="request-approval-001",
    )

    assert result.status == "confirmation_required"
    assert captured == {
        "tool_name": CREATE_NUTRITION_MEAL_RECORD,
        "arguments": expected_record_arguments,
        "result_tool_name": REQUEST_RECORD_APPROVAL,
    }


@pytest.mark.asyncio
async def test_side_effect_approval_returns_survey_required_before_completion() -> None:
    server = AgentMcpToolServer(
        backend_client=FakeBackendV13Client(),
        backend_queries=FakeBackendQueryTools(),
    )
    payload = v13_payload()
    payload["context"]["trusted_patient_context"] = {
        "availability": {
            "today_medication": "available",
        },
        "active_medication_schedules": [],
    }

    result = await execute(
        server,
        name=REQUEST_RECORD_APPROVAL,
        arguments={
            "action_name": CREATE_MEDICATION_SIDE_EFFECT_RECORD,
            "record_arguments": {
                "symptom_text": "어제 약을 먹고 메스꺼웠어",
                "symptom_onset_text": "어제 복용 후",
            },
        },
        source_event_type="medication_agent",
        tool_call_id="request-side-effect-approval-too-early",
        payload=payload,
    )

    assert result.status == "success"
    assert result.error == ""
    assert result.response == {
        "approval_status": "survey_required",
        "approval_created": False,
        "record_applied": False,
        "required_state": "completed_pro_ctcae_survey",
        "message": "먼저 PRO-CTCAE 설문을 완료해야 합니다.",
    }
    with SessionLocal() as session:
        assert session.scalar(select(AgentPendingAction)) is None


@pytest.mark.asyncio
async def test_v13_policy_write_tool_requires_user_approval() -> None:
    client = FakeBackendV13Client()
    queries = FakeBackendQueryTools()
    server = AgentMcpToolServer(backend_client=client, backend_queries=queries)

    result = await execute(
        server,
        name=CHANGE_NOTIFICATION_POLICY,
        arguments={
            "policy_id": "npol_0000000000000001",
            "decision": "apply",
            "changes": {
                "extra_reminders": 2,
                "interval_minutes": 15,
            },
        },
        source_event_type="multiturn_chat",
        tool_call_id="tool-call-policy-001",
    )

    assert result.status == "error"
    assert result.error == "record_approval_required"
    assert result.response["required_tool"] == REQUEST_RECORD_APPROVAL
    assert client.policy_requests == []
    assert queries.version_queries == []


@pytest.mark.asyncio
async def test_v13_approved_policy_write_uses_internal_approval_capability() -> None:
    client = FakeBackendV13Client()
    queries = FakeBackendQueryTools()
    server = AgentMcpToolServer(backend_client=client, backend_queries=queries)
    payload = v13_payload()
    payload["_approved_action_name"] = CHANGE_NOTIFICATION_POLICY
    payload["_approved_arguments"] = {
        "policy_id": "npol_0000000000000001",
        "decision": "apply",
        "changes": {
            "extra_reminders": 2,
            "interval_minutes": 15,
        },
    }
    approval_key = seed_internal_approval(
        server,
        payload,
        tool_call_id="approved-policy-confirmation-001",
    )

    result = await execute(
        server,
        name=CHANGE_NOTIFICATION_POLICY,
        arguments={"approval_key": approval_key},
        source_event_type="multiturn_chat",
        tool_call_id="approved-policy-confirmation-001",
        payload=payload,
    )

    assert result.status == "success", result.model_dump()
    assert len(client.policy_requests) == 1
    request = client.policy_requests[0]
    assert request.policy_id == "npol_0000000000000001"
    assert request.expected_version == 4
    assert request.payload.decision == "apply"
    assert request.payload.changes is not None
    assert request.payload.changes.interval_minutes == 15
    assert request.payload.reason == "사용자 채팅 메시지에서 명시적으로 확인된 AI Tool 실행"
    assert queries.version_queries == [
        {
            "patient_id": PATIENT_ID,
            "resource_type": "notification_policy",
            "record_id": "npol_0000000000000001",
        }
    ]
    assert request.confirmation_message_id == SOURCE_MESSAGE_ID
    assert result.idempotency_key
    with SessionLocal() as session:
        approval = session.scalar(select(AgentPendingAction))
        assert approval is not None
        assert approval.status == CONSUMED


@pytest.mark.asyncio
async def test_v13_update_write_tool_rejects_missing_internal_approval() -> None:
    client = FakeBackendV13Client()
    queries = FakeBackendQueryTools()
    server = AgentMcpToolServer(backend_client=client, backend_queries=queries)

    result = await execute(
        server,
        name=UPDATE_NUTRITION_MEAL_RECORD,
        arguments={
            "meal_id": "meal_0000000000000010",
            "description": "점심 기록 정정",
        },
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-update-001",
    )
    assert result.status == "error"
    assert result.error == "record_approval_required"
    assert client.record_requests == []
    assert queries.version_queries == []


@pytest.mark.asyncio
async def test_v13_update_write_tool_uses_internal_approval_capability() -> None:
    client = FakeBackendV13Client()
    queries = FakeBackendQueryTools()
    server = AgentMcpToolServer(backend_client=client, backend_queries=queries)
    payload = v13_payload()
    payload["_approved_action_name"] = UPDATE_NUTRITION_MEAL_RECORD
    payload["_approved_arguments"] = {
        "meal_id": "meal_0000000000000010",
        "description": "점심 기록 정정",
    }
    approval_key = seed_internal_approval(
        server,
        payload,
        tool_call_id="approved-confirmation-001",
    )

    result = await execute(
        server,
        name=UPDATE_NUTRITION_MEAL_RECORD,
        arguments={"approval_key": approval_key},
        source_event_type="nutrition_management_agent",
        tool_call_id="approved_confirmation-001",
        payload=payload,
    )

    assert result.status == "success", result.model_dump()
    assert len(client.record_requests) == 1
    request = client.record_requests[0]
    assert request.expected_version == 7
    assert request.confirmation_message_id == SOURCE_MESSAGE_ID
    assert request.payload.description == "점심 기록 정정"
    assert request.payload.reason == "사용자 채팅 메시지에서 명시적으로 확인된 AI Tool 실행"
    assert result.idempotency_key
    with SessionLocal() as session:
        approval = session.scalar(select(AgentPendingAction))
        assert approval is not None
        assert approval.status == CONSUMED


@pytest.mark.asyncio
async def test_v13_write_tool_rejects_model_supplied_technical_arguments() -> None:
    client = FakeBackendV13Client()
    server = AgentMcpToolServer(
        backend_client=client,
        backend_queries=FakeBackendQueryTools(),
    )

    result = await execute(
        server,
        name=UPDATE_NUTRITION_MEAL_RECORD,
        arguments={
            "meal_id": "meal_0000000000000010",
            "description": "점심 기록 정정",
            "patient_id": "patient_00000000ffffffff",
            "expected_version": 999,
        },
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-update-technical-001",
    )

    assert result.status == "error"
    assert result.error == "tool_permission_denied"
    assert client.record_requests == []


@pytest.mark.asyncio
async def test_policy_query_returns_public_id_from_trusted_patient_context() -> None:
    queries = FakeBackendQueryTools()
    server = AgentMcpToolServer(
        backend_client=FakeBackendV13Client(),
        backend_queries=queries,
    )

    result = await execute(
        server,
        name=GET_NOTIFICATION_POLICIES,
        arguments={"slot_label": "아침"},
        source_event_type="multiturn_chat",
        tool_call_id="tool-call-policy-query-001",
    )

    assert result.status == "success"
    assert result.response["policies"][0]["policy_id"] == "npol_0000000000000001"


@pytest.mark.asyncio
async def test_v13_side_effect_write_rejects_model_generated_assessment_fields() -> None:
    client = FakeBackendV13Client()
    server = AgentMcpToolServer(
        backend_client=client,
        backend_queries=FakeBackendQueryTools(),
    )

    result = await execute(
        server,
        name=CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        arguments={
            "symptom_text": "메스꺼움",
            "suspected": True,
            "severity": "low",
        },
        source_event_type="medication_agent",
        tool_call_id="tool-call-side-effect-001",
    )

    assert result.status == "error"
    assert result.error == "unsupported_model_arguments"
    assert client.record_requests == []
