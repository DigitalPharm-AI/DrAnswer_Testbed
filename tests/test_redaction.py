from __future__ import annotations

import json

from shared.redaction import redact_for_logging, redact_inline_secrets, safe_exception_summary, safe_log_arguments
from shared.schemas import AgentNotificationRequest, ToolCallResult
from system_app.models import BackendApiRequest, ChatMessage, Notification
from system_app.services.agent_callback_service import process_agent_notification_callback
from shared.tool_names import CREATE_NUTRITION_MEAL_RECORD, UPDATE_MEDICATION_DOSE_EVENT_STATUS
from agent_app.tools.protocol import mcp_result_from_json_rpc_response, mcp_result_from_tool_result, tool_result_from_mcp_result
from agent_app.tools.results import tool_calls_payload, tool_result_summary
from tests.helpers import build_session


def test_redact_for_logging_hashes_identifiers_and_redacts_clinical_text():
    payload = redact_for_logging(
        {
            "patient_id": "patient-demo-001",
            "medical_record_number": "mrn-secret-value",
            "message": "속이 메스꺼워요",
            "medication_name": "항암제",
            "nested": {"authorization": "Bearer token-value"},
            "token_usage": {"input_tokens": 120, "output_tokens": 80},
            "safe_status": "ok",
        }
    )

    assert payload["patient_id"]["type"] == "identifier"
    assert payload["patient_id"]["sha256"]
    assert payload["medical_record_number"]["type"] == "identifier"
    assert payload["message"]["type"] == "clinical_text"
    assert payload["medication_name"]["type"] == "clinical_text"
    assert payload["nested"]["authorization"]["type"] == "secret"
    assert payload["token_usage"] == {"input_tokens": 120, "output_tokens": 80}
    assert payload["safe_status"] == "ok"
    assert "patient-demo-001" not in str(payload)
    assert "mrn-secret-value" not in str(payload)
    assert "속이 메스꺼워요" not in str(payload)


def test_redact_inline_secrets_masks_common_secret_and_contact_patterns():
    text = "email user@example.com phone 010-1234-5678 Authorization: Bearer abc.def token=secret-value"

    redacted = redact_inline_secrets(text)

    assert "user@example.com" not in redacted
    assert "010-1234-5678" not in redacted
    assert "abc.def" not in redacted
    assert "secret-value" not in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_PHONE]" in redacted


def test_redact_for_logging_covers_agent_trace_text_keys():
    payload = redact_for_logging(
        {
            "summary": "patient asked about nausea after dinner",
            "result": "recommended avoiding soy soup",
            "result_message": "meal preference saved",
            "query": "soy allergy lunch",
            "object_label": "soy",
            "input_symptom": "nausea",
            "matched_symptom_term": "Nausea",
            "matched_korean_symptom_name": "메스꺼움",
            "description": "ate jajangmyeon for lunch",
            "reason": "patient explicitly dislikes jajangmyeon",
            "error": "tool failed while processing peanut allergy details",
            "error_message": "provider failed with private symptom payload",
            "last_error": "callback failed with private meal payload",
            "safe_status": "ok",
        }
    )

    for key in (
        "summary",
        "result",
        "result_message",
        "query",
        "object_label",
        "input_symptom",
        "matched_symptom_term",
        "matched_korean_symptom_name",
        "description",
        "reason",
        "error",
        "error_message",
        "last_error",
    ):
        assert payload[key]["type"] == "clinical_text"
        assert payload[key]["sha256"]

    assert payload["safe_status"] == "ok"
    rendered = str(payload)
    assert "soy allergy lunch" not in rendered
    assert "ate jajangmyeon" not in rendered
    assert "Nausea" not in rendered
    assert "private symptom payload" not in rendered


def test_safe_log_arguments_uses_central_redaction_and_compacts_payloads():
    payload = safe_log_arguments(
        {
            "patient_id": "patient-demo-001",
            "medical_record_number": "mrn-secret-value",
            "object_label": "soy",
            "query": "soy allergy lunch",
            "foods": [{"food_name": "soy soup", "nutrients": {"sodium": 500}}],
            "metadata": {"free_text": "not expanded in compact log"},
            "api_key": "secret-api-key",
            "approval_key": "apv_private-capability",
            "limit": 5,
        }
    )

    assert payload["patient_id"]["type"] == "identifier"
    assert payload["medical_record_number"]["type"] == "identifier"
    assert payload["object_label"]["type"] == "clinical_text"
    assert payload["query"]["type"] == "clinical_text"
    assert payload["foods"] == {"count": 1}
    assert payload["metadata"] == {"keys": ["free_text"]}
    assert payload["api_key_present"] is True
    assert payload["approval_key_present"] is True
    assert payload["limit"] == 5

    rendered = str(payload)
    assert "patient-demo-001" not in rendered
    assert "mrn-secret-value" not in rendered
    assert "soy allergy lunch" not in rendered
    assert "soy soup" not in rendered
    assert "secret-api-key" not in rendered
    assert "apv_private-capability" not in rendered


def test_safe_exception_summary_redacts_raw_exception_text():
    exc = ValueError("pytest private exception peanut allergy token=secret-value 010-1234-5678")

    summary = safe_exception_summary(exc)

    assert summary.startswith("ValueError: clinical text redacted")
    assert "pytest private exception" not in summary
    assert "peanut allergy" not in summary
    assert "secret-value" not in summary
    assert "010-1234-5678" not in summary
    assert "sha256" in summary


def test_tool_result_summary_redacts_raw_tool_error_but_keeps_error_codes():
    raw_error = "pytest private tool error for peanut allergy and lunch preference"

    summary = tool_result_summary(
        [ToolCallResult(tool_name=CREATE_NUTRITION_MEAL_RECORD, status="error", error=raw_error)],
        "fallback",
    )
    code_summary = tool_result_summary(
        [ToolCallResult(tool_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS, status="error", error="tool_permission_denied")],
        "fallback",
    )

    assert summary.startswith("clinical text redacted")
    assert raw_error not in summary
    assert code_summary == "tool_permission_denied"


def test_mcp_error_tool_result_sanitizes_response_and_content():
    raw_detail = "pytest private MCP detail about peanut allergy and phone 010-2222-3333"
    result = ToolCallResult(
        tool_name=CREATE_NUTRITION_MEAL_RECORD,
        status="error",
        response={"detail": raw_detail, "source_event_type": "mcp", "elapsed_ms": 12},
        error=raw_detail,
        idempotency_key=f"trace:{CREATE_NUTRITION_MEAL_RECORD}",
    )

    mcp_result = mcp_result_from_tool_result(result)
    restored = tool_result_from_mcp_result(CREATE_NUTRITION_MEAL_RECORD, mcp_result)
    rendered = json.dumps(mcp_result, ensure_ascii=False)

    assert mcp_result["isError"] is True
    assert mcp_result["structuredContent"]["response"]["source_event_type"] == "mcp"
    assert mcp_result["structuredContent"]["response"]["detail"]["type"] == "clinical_text"
    assert mcp_result["structuredContent"]["error"].startswith("clinical text redacted")
    assert restored.response["detail"]["type"] == "clinical_text"
    assert raw_detail not in rendered
    assert "010-2222-3333" not in rendered


def test_mcp_json_rpc_error_response_sanitizes_message_and_data():
    raw_message = "pytest private json rpc failure about nausea and peanut allergy"

    mcp_result = mcp_result_from_json_rpc_response(
        {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": raw_message, "data": {"detail": raw_message, "status": "failed"}},
        }
    )
    rendered = json.dumps(mcp_result, ensure_ascii=False)

    assert mcp_result["isError"] is True
    assert mcp_result["structuredContent"]["error"].startswith("clinical text redacted")
    assert mcp_result["structuredContent"]["response"]["detail"]["type"] == "clinical_text"
    assert mcp_result["structuredContent"]["response"]["status"] == "failed"
    assert raw_message not in rendered


def test_agent_tool_calls_payload_redacts_error_response_but_keeps_success_response():
    raw_error = "pytest private tool payload about soybean allergy lunch failure"

    payload = tool_calls_payload(
        [],
        [
            ToolCallResult(
                tool_name=CREATE_NUTRITION_MEAL_RECORD,
                status="error",
                response={"detail": raw_error, "source_event_type": "multiturn_chat"},
                error=raw_error,
            ),
            ToolCallResult(
                tool_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
                status="success",
                response={"status": "taken", "message": "복용 완료로 기록했습니다."},
            ),
        ],
    )
    rendered = json.dumps(payload, ensure_ascii=False)

    error_result = payload["tool_results"][0]
    success_result = payload["tool_results"][1]
    assert error_result["response"]["detail"]["type"] == "clinical_text"
    assert error_result["error"].startswith("clinical text redacted")
    assert error_result["response"]["source_event_type"] == "multiturn_chat"
    assert success_result["response"]["message"] == "복용 완료로 기록했습니다."
    assert raw_error not in rendered


def test_agent_notification_callback_keeps_only_minimal_idempotency_receipt():
    raw_body = "pytest callback private allergy body"
    with build_session() as session:
        result = process_agent_notification_callback(
            session,
            AgentNotificationRequest(
                title="pytest callback title",
                body=raw_body,
                notification_type="conversation_alert",
                metadata={"status": "agent_ready", "message": raw_body},
                chat_category="side_effect",
                idempotency_key="pytest-callback-redaction-key",
            ),
        )

        notification = session.get(Notification, result["notification_id"])
        chat_message = session.query(ChatMessage).filter(ChatMessage.category == "side_effect").one()
        receipt = session.query(BackendApiRequest).filter(
            BackendApiRequest.api_path == "/api/agent/notifications",
            BackendApiRequest.request_id
            == "pytest-callback-redaction-key",
        ).one()

    assert notification is not None
    assert notification.body == raw_body
    assert chat_message.content == raw_body
    assert receipt.request_hash
    assert raw_body not in receipt.response_json
