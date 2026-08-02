from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from agent_app.tools.mcp_server import AgentMcpToolServer
from agent_app.tools.protocol import tool_result_from_mcp_result
from shared.backend_v13_contracts import (
    RecordChangeResponse,
    RecordChangeResult,
)
from shared.schemas import AEProCtcaeAssessmentRequest
from shared.tool_catalog import ToolCatalog
from shared.tool_names import (
    ALL_TOOL_NAMES,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NOTIFICATION_POLICIES,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)
from shared.tool_permissions import TOOL_ALLOWLIST, validate_tool_permission
from tests.support.adverse_reactions import TestbedAdverseReactionLookup

PATIENT_A = "patient_0000000000000001"
PATIENT_B = "patient_0000000000000002"

PATIENT_SCOPED_READ_TOOLS = {
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NOTIFICATION_POLICIES,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_SIDE_EFFECT_HISTORY,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
}


class FakeBackendClient:
    pass


class CapturingSideEffectWriteClient:
    def __init__(self) -> None:
        self.requests = []

    async def change_record(self, request):
        self.requests.append(request)
        return RecordChangeResponse(
            request_id=request.request_id,
            result=RecordChangeResult(
                resource_type="medication_side_effect",
                operation="create",
                record_id="sidefx_0000000000000001",
                version=1,
            ),
            processed_at=datetime.now(UTC),
        )


class CapturingBackendQueries:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _capture(self, method: str, arguments: dict) -> None:
        self.calls.append((method, arguments))

    def medication_dose_status(self, **arguments):
        self._capture("medication_dose_status", arguments)
        return {
            "success": True,
            "patient_id": arguments["patient_id"],
            "start_date": date(2026, 7, 25),
            "end_date": date(2026, 7, 25),
            "dose_events": [],
            "total": 0,
        }

    def nutrition_meals(self, **arguments):
        self._capture("nutrition_meals", arguments)
        return {"success": True, "meals": [], "total": 0}

    def daily_nutrition_summary(self, **arguments):
        self._capture("daily_nutrition_summary", arguments)
        return {"success": True, "daily_summary": {}}

    def nutrition_preferences(self, **arguments):
        self._capture("nutrition_preferences", arguments)
        return {"success": True, "preferences": {}}

    def recommendation_candidates(self, **arguments):
        self._capture("recommendation_candidates", arguments)
        return {"success": True, "recommendations": []}

    def side_effect_history(self, **arguments):
        self._capture("side_effect_history", arguments)
        return {"success": True, "records": [], "total": 0}

    def notification_policies(self, **arguments):
        self._capture("notification_policies", arguments)
        return {"success": True, "policies": [], "total": 0}

    def search_food_candidates(self, **arguments):
        self._capture("search_food_candidates", arguments)
        return {
            "success": True,
            "candidates": [],
            "query": arguments["query"],
            "limit": arguments["limit"],
        }


def _v13_payload() -> dict:
    return {
        "patient_id": PATIENT_A,
        "current_time": "2026-04-20T14:30:00+09:00",
        "context": {
            "request_metadata": {
                "contract_version": "v1.3",
                "request_id": "req_0000000000000001",
                "message_id": "user_msg_0000000000000001",
            }
        },
    }


async def _execute(
    server: AgentMcpToolServer,
    *,
    tool_name: str,
    arguments: dict,
    source_event_type: str,
    payload: dict | None = None,
):
    mcp_result = await server.tools_call(
        {
            "name": tool_name,
            "arguments": arguments,
            "tool_call_id": f"call-{tool_name}",
        },
        trace_id="trace-patient-scope",
        source_event_type=source_event_type,
        payload=payload or _v13_payload(),
    )
    return tool_result_from_mcp_result(tool_name, mcp_result)


def test_patient_scoped_read_catalog_hides_patient_id_and_rejects_extra_arguments() -> None:
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}

    for tool in tools.values():
        assert "patient_id" not in tool["required_arguments"]
        assert "patient_id" not in tool["optional_arguments"]
        assert "patient_id" not in tool["inputSchema"]["properties"]
        assert tool["inputSchema"]["additionalProperties"] is False

    for tool_name in PATIENT_SCOPED_READ_TOOLS:
        tool = tools[tool_name]
        assert tool["args_schema"] == tool["inputSchema"]


def test_pro_ctcae_model_contract_accepts_only_patient_symptom_text() -> None:
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}
    tool = tools[GET_PRO_CTCAE_QUESTIONNAIRE]

    assert tool["required_arguments"] == ["symptom_text"]
    assert tool["optional_arguments"] == []
    assert tool["inputSchema"]["required"] == ["symptom_text"]
    assert set(tool["inputSchema"]["properties"]) == {"symptom_text"}
    assert tool["inputSchema"]["additionalProperties"] is False
    assert AEProCtcaeAssessmentRequest.model_validate(
        {"symptom_text": "속이 메스꺼워요"}
    ).symptom_text == "속이 메스꺼워요"

    with pytest.raises(ValidationError):
        AEProCtcaeAssessmentRequest.model_validate(
            {
                "symptom_text": "속이 메스꺼워요",
                "symptom_normalize": "메스꺼움",
            }
        )

    with pytest.raises(ValidationError):
        AEProCtcaeAssessmentRequest.model_validate(
            {
                "symptom_text": "속이 메스꺼워요",
                "threshold": 0.1,
            }
        )


def test_pro_ctcae_server_applies_matching_without_model_threshold(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeAssessment:
        @staticmethod
        def model_dump(*, mode: str) -> dict:
            assert mode == "json"
            return {"input_symptom": "속이 메스꺼워요", "matched": True}

    def fake_match(symptom_text: str, **options):
        captured["symptom_text"] = symptom_text
        captured["options"] = options
        return FakeAssessment()

    monkeypatch.setattr(
        "agent_app.tools.mcp_server.match_pro_ctcae_symptom",
        fake_match,
    )

    result = AgentMcpToolServer._ae_pro_ctcae(
        {"symptom_text": "속이 메스꺼워요"},
        trace_id="trace-pro-server-managed",
    )

    assert captured == {
        "symptom_text": "속이 메스꺼워요",
        "options": {},
    }
    assert result.status == "success"
    assert result.response["matched"] is True


def test_catalog_hides_ai_server_managed_write_arguments() -> None:
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}

    dose_tool = tools[UPDATE_MEDICATION_DOSE_EVENT_STATUS]
    delete_food_tool = tools[DELETE_NUTRITION_FOOD_RECORD]
    side_effect_tool = tools[CREATE_MEDICATION_SIDE_EFFECT_RECORD]

    assert "taken_at" not in dose_tool["optional_arguments"]
    assert "taken_at" not in dose_tool["inputSchema"]["properties"]
    assert "delete_empty_meal" not in delete_food_tool["optional_arguments"]
    assert "delete_empty_meal" not in delete_food_tool["inputSchema"]["properties"]
    assert side_effect_tool["inputSchema"]["additionalProperties"] is False
    assert set(side_effect_tool["inputSchema"]["properties"]) == {
        "symptom_text",
        "symptom_onset_text",
        "medication_name",
    }
    assert side_effect_tool["required_arguments"] == ["symptom_text"]


def test_v13_rejects_model_supplied_patient_id_for_every_executable_tool() -> None:
    tested_tools: set[str] = set()

    for source_event_type, tool_names in TOOL_ALLOWLIST.items():
        for tool_name in tool_names:
            denial = validate_tool_permission(
                {
                    "name": tool_name,
                    "arguments": {"patient_id": PATIENT_B},
                },
                source_event_type=source_event_type,
                payload=_v13_payload(),
            )
            assert denial == f"{tool_name} patient_id is managed inside the AI Server Tool"
            tested_tools.add(tool_name)

    assert tested_tools == set(ALL_TOOL_NAMES)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "source_event_type", "safe_arguments", "query_method"),
    [
        (
            GET_MEDICATION_DOSE_STATUS,
            "medication_agent",
            {"target_date": "2026-07-25"},
            "medication_dose_status",
        ),
        (
            GET_SIDE_EFFECT_HISTORY,
            "medication_agent",
            {"limit": 20},
            "side_effect_history",
        ),
        (
            GET_NUTRITION_MEAL_RECORD_LIST,
            "nutrition_management_agent",
            {"meal_date": "2026-07-25"},
            "nutrition_meals",
        ),
        (
            GET_NUTRITION_DAILY_SUMMARY,
            "nutrition_management_agent",
            {"meal_date": "2026-07-25"},
            "daily_nutrition_summary",
        ),
        (
            GET_NUTRITION_PREFERENCE_SUMMARY,
            "nutrition_management_agent",
            {},
            "nutrition_preferences",
        ),
        (
            GET_NUTRITION_RECOMMENDATION_CANDIDATES,
            "nutrition_recommendation_agent",
            {"constraints": {"sodium": "low"}},
            "recommendation_candidates",
        ),
        (
            GET_NOTIFICATION_POLICIES,
            "multiturn_chat",
            {"active_only": True},
            "notification_policies",
        ),
    ],
)
async def test_v13_cross_patient_argument_is_denied_and_trusted_patient_reaches_query(
    tool_name: str,
    source_event_type: str,
    safe_arguments: dict,
    query_method: str,
) -> None:
    queries = CapturingBackendQueries()
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=queries,
    )

    forged = await _execute(
        server,
        tool_name=tool_name,
        arguments={**safe_arguments, "patient_id": PATIENT_B},
        source_event_type=source_event_type,
    )

    assert forged.status == "error"
    assert forged.error == "tool_permission_denied"
    assert queries.calls == []

    allowed = await _execute(
        server,
        tool_name=tool_name,
        arguments=safe_arguments,
        source_event_type=source_event_type,
    )

    assert allowed.status == "success"
    assert len(queries.calls) == 1
    assert queries.calls[0][0] == query_method
    assert queries.calls[0][1]["patient_id"] == PATIENT_A
    assert PATIENT_B not in str(queries.calls[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "source_event_type", "arguments"),
    [
        (GET_MEDICATION_DOSE_STATUS, "medication_agent", {}),
        (GET_SIDE_EFFECT_HISTORY, "medication_agent", {"limit": 20}),
        (
            SEARCH_NUTRITION_FOOD_CANDIDATES,
            "nutrition_recommendation_agent",
            {"food_queries": ["rice"]},
        ),
        (
            GET_NUTRITION_MEAL_RECORD_LIST,
            "nutrition_management_agent",
            {"meal_date": "2026-07-25"},
        ),
        (
            GET_NUTRITION_DAILY_SUMMARY,
            "nutrition_management_agent",
            {"meal_date": "2026-07-25"},
        ),
        (GET_NUTRITION_PREFERENCE_SUMMARY, "nutrition_management_agent", {}),
        (
            GET_NUTRITION_RECOMMENDATION_CANDIDATES,
            "nutrition_recommendation_agent",
            {"constraints": {"sodium": "low"}},
        ),
    ],
)
async def test_read_tools_do_not_fallback_to_backend_http_api(
    monkeypatch,
    tool_name: str,
    source_event_type: str,
    arguments: dict,
) -> None:
    queries = CapturingBackendQueries()
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=queries,
    )
    server.backend_queries = None

    def reject_http_fallback(*_args, **_kwargs):
        raise AssertionError("read Tool must not call /api/agent/*")

    monkeypatch.setattr(
        "agent_app.tools.mcp_server.httpx.AsyncClient",
        reject_http_fallback,
    )

    result = await _execute(
        server,
        tool_name=tool_name,
        arguments=arguments,
        source_event_type=source_event_type,
    )

    assert result.status == "error"
    assert result.error == "backend_read_query_tools_not_configured"
    assert queries.calls == []


@pytest.mark.asyncio
async def test_medication_status_tool_injects_trusted_simulation_date() -> None:
    queries = CapturingBackendQueries()
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=queries,
    )

    result = await _execute(
        server,
        tool_name=GET_MEDICATION_DOSE_STATUS,
        arguments={},
        source_event_type="medication_agent",
    )

    assert result.status == "success"
    assert queries.calls == [
        (
            "medication_dose_status",
            {
                "patient_id": PATIENT_A,
                "target_date": "2026-04-20",
            },
        )
    ]


@pytest.mark.asyncio
async def test_v13_food_search_rejects_patient_override_and_does_not_forward_it() -> None:
    queries = CapturingBackendQueries()
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=queries,
    )

    forged = await _execute(
        server,
        tool_name=SEARCH_NUTRITION_FOOD_CANDIDATES,
        arguments={
            "food_queries": ["rice"],
            "patient_id": PATIENT_B,
        },
        source_event_type="nutrition_recommendation_agent",
    )

    assert forged.status == "error"
    assert forged.error == "tool_permission_denied"
    assert queries.calls == []

    allowed = await _execute(
        server,
        tool_name=SEARCH_NUTRITION_FOOD_CANDIDATES,
        arguments={
            "food_queries": ["rice", "RICE", "egg"],
            "limit_per_query": 4,
        },
        source_event_type="nutrition_recommendation_agent",
    )

    assert allowed.status == "success"
    assert [
        group["query"]
        for group in allowed.response["search_groups"]
    ] == ["rice", "egg"]
    assert allowed.response["limit_per_query"] == 4
    assert queries.calls == [
        (
            "search_food_candidates",
            {
                "query": "rice",
                "limit": 4,
            },
        ),
        (
            "search_food_candidates",
            {
                "query": "egg",
                "limit": 4,
            },
        )
    ]
    assert (
        server._patient_id_for_tool(
            {"patient_id": PATIENT_B},
            _v13_payload(),
        )
        == PATIENT_A
    )


@pytest.mark.asyncio
async def test_side_effect_assessment_uses_trusted_snapshot_without_phr_key() -> None:
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=CapturingBackendQueries(),
        adverse_reactions=TestbedAdverseReactionLookup(),
    )
    payload = _v13_payload()
    payload["context"]["trusted_patient_context"] = {
        "patient_id": PATIENT_A,
        "as_of": "2026-07-25T10:30:00+09:00",
        "availability": {"today_medication": "available"},
        "active_medication_schedules": [
            {
                "medication_name": "메트포르민 500mg",
                "treatment_area": "당뇨약",
            }
        ],
        "today_medication": {
            "dose_events": [
                {
                    "dose_event_id": "dose_metformin",
                    "medication_name": "메트포르민 500mg",
                    "scheduled_for": "2026-07-25T08:00:00+09:00",
                    "version": 2,
                }
            ]
        },
    }

    result = await _execute(
        server,
        tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
        arguments={
            "symptom_mentions": [
                {
                    "text": "속이 메스꺼웠어",
                    "onset_text": "어제 복용 후",
                }
            ],
        },
        source_event_type="medication_agent",
        payload=payload,
    )

    assert result.status == "success"
    assert result.response["suspected"] is True
    assert "requires_medication_selection" not in result.response
    draft = result.response["side_effect_record_draft"]
    assert draft["medication_name"] is None
    assert draft["related_dose_event_id"] is None
    assert draft["symptom_onset_text"] == "어제 복용 후"
    assert "patient_id" not in draft
    assert "phr_patient_key" not in str(result.response)


@pytest.mark.asyncio
async def test_side_effect_assessment_keeps_all_matches_without_forced_choice() -> None:
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=CapturingBackendQueries(),
        adverse_reactions=TestbedAdverseReactionLookup(),
    )
    payload = _v13_payload()
    payload["context"]["trusted_patient_context"] = {
        "patient_id": PATIENT_A,
        "as_of": "2026-07-25T10:30:00+09:00",
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
        "today_medication": {"dose_events": []},
    }

    result = await _execute(
        server,
        tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
        arguments={
            "symptom_mentions": [{"text": "속이 메스꺼웠어"}]
        },
        source_event_type="medication_agent",
        payload=payload,
    )

    assert result.status == "success"
    assert "requires_medication_selection" not in result.response
    assert result.response["matched_items"] == [
        "메트포르민 500mg",
        "수니티닙 25mg",
    ]
    assert "medication_candidates" not in result.response
    draft = result.response["side_effect_record_draft"]
    assert draft["medication_name"] is None
    assert draft["matched_items"] == [
        "메트포르민 500mg",
        "수니티닙 25mg",
    ]
    assert draft["related_dose_event_id"] is None


@pytest.mark.asyncio
async def test_side_effect_assessment_accepts_multiple_raw_symptom_mentions() -> None:
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=CapturingBackendQueries(),
        adverse_reactions=TestbedAdverseReactionLookup(),
    )
    payload = _v13_payload()
    payload["context"]["trusted_patient_context"] = {
        "patient_id": PATIENT_A,
        "as_of": "2026-07-25T10:30:00+09:00",
        "availability": {"today_medication": "available"},
        "active_medication_schedules": [
            {"medication_name": "메트포르민 500mg"},
            {"medication_name": "암로디핀 5mg"},
        ],
        "today_medication": {"dose_events": []},
    }

    result = await _execute(
        server,
        tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
        arguments={
            "symptom_mentions": [
                {
                    "text": "속이 울렁거렸어",
                    "onset_text": "어제 복용 후",
                },
                {
                    "text": "어지러웠어",
                    "onset_text": "오늘 아침",
                },
            ]
        },
        source_event_type="medication_agent",
        payload=payload,
    )

    assert result.status == "success"
    assert [
        item["symptom_text"] for item in result.response["assessments"]
    ] == ["속이 울렁거렸어", "어지러웠어"]
    assert [
        item["symptom_onset_text"]
        for item in result.response["assessments"]
    ] == ["어제 복용 후", "오늘 아침"]
    assert result.response["matched_items"] == [
        "메트포르민 500mg",
        "암로디핀 5mg",
    ]
    assert len(result.response["side_effect_record_drafts"]) == 2
    assert "side_effect_record_draft" not in result.response


@pytest.mark.asyncio
async def test_confirmed_side_effect_write_uses_backend_sync_contract_once() -> None:
    client = CapturingSideEffectWriteClient()
    server = AgentMcpToolServer(
        backend_client=client,
        backend_queries=CapturingBackendQueries(),
        adverse_reactions=TestbedAdverseReactionLookup(),
    )
    source_chat_request_id = "req_0000000000000002"
    confirmation_message_id = "user_msg_0000000000000002"
    authoritative_arguments = {
        "medication_name": "메트포르민 500mg",
        "symptom_text": "속이 메스꺼웠어",
        "symptom_onset_text": "어제 복용 후",
        "suspected": True,
        "severity": {
            "questions": [
                {
                    "item_code": "PROCTCAE_NAUSEA",
                    "question": "지난 7일 동안 메스꺼움의 정도는 어떠했습니까?",
                    "response_type": "single_choice",
                    "response_options": [
                        "없음", "약간", "중간 정도", "심함", "매우 심함"
                    ],
                }
            ],
            "responses": [
                {
                    "item_code": "PROCTCAE_NAUSEA",
                    "response_index": 2,
                    "response_text": "중간 정도",
                }
            ],
        },
        "matched_effects": ["메스꺼움"],
        "matched_items": ["메트포르민 500mg"],
        "related_dose_event_id": "dose_0000000000000001",
    }
    display = {
        "title": "부작용 평가 기록",
        "question": "평가 결과를 기록할까요?",
        "action_label": "기록",
    }
    server.backend_writes.approval_store.prepare(
        patient_id=PATIENT_A,
        source_chat_request_id="req_0000000000000001",
        source_message_id="user_msg_0000000000000001",
        trace_id="trace-side-effect-approval",
        action_name=CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        tool_call_id="call-create-side-effect",
        arguments=authoritative_arguments,
        display=display,
    )
    decision = server.backend_writes.approval_store.submit_response(
        patient_id=PATIENT_A,
        current_user_message_id=confirmation_message_id,
        current_source_chat_request_id=source_chat_request_id,
        originating_user_message_id="user_msg_0000000000000001",
        submitted_value="기록",
        source_message={
            "message_type": "selection_box",
            "message": {
                "message_title": display["title"],
                "text": display["question"],
                "selections": ["기록", "취소"],
            },
        },
    )
    assert decision is not None and decision.kind == "approved"

    payload = _v13_payload()
    payload["context"]["request_metadata"].update(
        {
            "request_id": source_chat_request_id,
            "message_id": confirmation_message_id,
        }
    )
    payload["context"]["approved_user_action"] = {
        "status": "confirmed",
        "action_name": CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        "arguments": {"approval_key": decision.approval_key},
    }

    result = await _execute(
        server,
        tool_name=CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        arguments={"approval_key": decision.approval_key},
        source_event_type="medication_agent",
        payload=payload,
    )

    assert result.status == "success"
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.request_id
    assert request.source_chat_request_id == source_chat_request_id
    assert request.confirmation_message_id == confirmation_message_id
    assert request.patient_id == PATIENT_A
    assert request.resource_type == "medication_side_effect"
    assert request.operation == "create"
    assert request.record_id is None
    assert request.parent_record_id is None
    assert request.expected_version is None
    assert request.payload.symptom_text == "속이 메스꺼웠어"
    assert request.payload.suspected is True
    assert request.payload.related_dose_event_id == "dose_0000000000000001"
    assert set(request.payload.model_dump(mode="json")) == {
        "medication_name",
        "symptom_text",
        "symptom_onset_text",
        "suspected",
        "severity",
        "matched_effects",
        "matched_items",
        "related_dose_event_id",
    }
    assert "approval_key" not in request.model_dump(mode="json")
    assert "source_trace_id" not in request.model_dump(mode="json")
