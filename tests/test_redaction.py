from __future__ import annotations

import json

from shared.json_utils import parse_json_object
from shared.redaction import redact_for_logging, redact_inline_secrets, safe_exception_summary, safe_log_arguments
from shared.schemas import AgentNotificationRequest, AgentResponse, ToolCallResult
from system_app.models import AgentDecisionAudit, ChatMessage, Notification
from system_app.services.agent_callback_service import process_agent_notification_callback
from system_app.services.audit_service import create_agent_decision_audit, record_agent_audit
from agent_app.tool_protocol import mcp_result_from_json_rpc_response, mcp_result_from_tool_result, tool_result_from_mcp_result
from agent_app.tool_results import tool_calls_payload, tool_result_summary
from tests.helpers import build_session


def test_redact_for_logging_hashes_identifiers_and_redacts_clinical_text():
    payload = redact_for_logging(
        {
            "patient_id": "patient-demo-001",
            "phr_patient_key": "phr-secret-key",
            "message": "속이 메스꺼워요",
            "medication_name": "항암제",
            "nested": {"authorization": "Bearer token-value"},
            "token_usage": {"input_tokens": 120, "output_tokens": 80},
            "safe_status": "ok",
        }
    )

    assert payload["patient_id"]["type"] == "identifier"
    assert payload["patient_id"]["sha256"]
    assert payload["phr_patient_key"]["type"] == "identifier"
    assert payload["message"]["type"] == "clinical_text"
    assert payload["medication_name"]["type"] == "clinical_text"
    assert payload["nested"]["authorization"]["type"] == "secret"
    assert payload["token_usage"] == {"input_tokens": 120, "output_tokens": 80}
    assert payload["safe_status"] == "ok"
    assert "patient-demo-001" not in str(payload)
    assert "phr-secret-key" not in str(payload)
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
            "phr_patient_key": "phr-secret-key",
            "object_label": "soy",
            "query": "soy allergy lunch",
            "foods": [{"food_name": "soy soup", "nutrients": {"sodium": 500}}],
            "metadata": {"free_text": "not expanded in compact log"},
            "api_key": "secret-api-key",
            "limit": 5,
        }
    )

    assert payload["patient_id"]["type"] == "identifier"
    assert payload["phr_patient_key"]["type"] == "identifier"
    assert payload["object_label"]["type"] == "clinical_text"
    assert payload["query"]["type"] == "clinical_text"
    assert payload["foods"] == {"count": 1}
    assert payload["metadata"] == {"keys": ["free_text"]}
    assert payload["api_key_present"] is True
    assert payload["limit"] == 5

    rendered = str(payload)
    assert "patient-demo-001" not in rendered
    assert "phr-secret-key" not in rendered
    assert "soy allergy lunch" not in rendered
    assert "soy soup" not in rendered
    assert "secret-api-key" not in rendered


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
        [ToolCallResult(tool_name="record_meal", status="error", error=raw_error)],
        "fallback",
    )
    code_summary = tool_result_summary(
        [ToolCallResult(tool_name="mark_dose_taken", status="error", error="tool_permission_denied")],
        "fallback",
    )

    assert summary.startswith("clinical text redacted")
    assert raw_error not in summary
    assert code_summary == "tool_permission_denied"


def test_mcp_error_tool_result_sanitizes_response_and_content():
    raw_detail = "pytest private MCP detail about peanut allergy and phone 010-2222-3333"
    result = ToolCallResult(
        tool_name="record_meal",
        status="error",
        response={"detail": raw_detail, "source_event_type": "mcp", "elapsed_ms": 12},
        error=raw_detail,
        idempotency_key="trace:record_meal",
    )

    mcp_result = mcp_result_from_tool_result(result)
    restored = tool_result_from_mcp_result("record_meal", mcp_result)
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
                tool_name="record_meal",
                status="error",
                response={"detail": raw_error, "source_event_type": "multiturn_chat"},
                error=raw_error,
            ),
            ToolCallResult(
                tool_name="mark_dose_taken",
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


def test_record_agent_audit_persists_redacted_summary_and_payload():
    with build_session() as session:
        response = AgentResponse(
            trace_id="trace-audit-redaction",
            agent_name="nutrition_agent",
            prompt_version_id="test",
            decision_type="tool_call",
            structured_payload={
                "tool_call": {
                    "name": "record_nutrition_preference",
                    "arguments": {
                        "patient_id": "patient-redaction-001",
                        "message": "pytest private nausea and peanut allergy",
                        "slot_label": "아침 08:00",
                    },
                },
                "advice": "pytest private advice about jajangmyeon",
                "tools_executed": True,
            },
            human_summary="pytest private summary about peanut allergy and jajangmyeon",
            requires_conversation_alert=False,
        )

        audit = record_agent_audit(session, response, "multiturn_chat", applied=False, error_message="token=secret-value")
        session.commit()

        payload = parse_json_object(audit.structured_payload)
        audit_summary = audit.human_summary
        audit_error = audit.error_message
        rendered = f"{audit.human_summary} {audit.structured_payload} {audit.error_message}"

    assert audit_summary.startswith("clinical text redacted")
    assert payload["tool_call"]["arguments"]["patient_id"]["type"] == "identifier"
    assert payload["tool_call"]["arguments"]["message"]["type"] == "clinical_text"
    assert payload["tool_call"]["arguments"]["slot_label"] == "아침 08:00"
    assert payload["advice"]["type"] == "clinical_text"
    assert "secret-value" not in audit_error
    assert "patient-redaction-001" not in rendered
    assert "pytest private nausea" not in rendered
    assert "pytest private advice" not in rendered
    assert "pytest private summary" not in rendered


def test_create_agent_decision_audit_redacts_direct_callers():
    with build_session() as session:
        audit = create_agent_decision_audit(
            session,
            trace_id="trace-direct-audit-redaction",
            agent_name="agent_async_callback",
            prompt_version_id="n/a",
            decision_type="agent_async_chat_result",
            structured_payload={
                "notification_id": 123,
                "patient_id": "patient-direct-001",
                "result_message": "pytest direct private symptom text",
            },
            human_summary="pytest direct private summary",
            applied=True,
            error_message="Authorization: Bearer private-token-value",
            source_event_type="agent_async_chat_result",
            flush=True,
        )
        session.commit()

        payload = parse_json_object(audit.structured_payload)
        rendered = f"{audit.human_summary} {audit.structured_payload} {audit.error_message}"

    assert payload["notification_id"] == 123
    assert payload["patient_id"]["type"] == "identifier"
    assert payload["result_message"]["type"] == "clinical_text"
    assert audit.human_summary.startswith("clinical text redacted")
    assert "private-token-value" not in audit.error_message
    assert "patient-direct-001" not in rendered
    assert "pytest direct private symptom" not in rendered
    assert "pytest direct private summary" not in rendered


def test_agent_notification_callback_redacts_audit_but_keeps_user_visible_message():
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
        audit = session.query(AgentDecisionAudit).filter(AgentDecisionAudit.trace_id == "pytest-callback-redaction-key").one()
        audit_payload = parse_json_object(audit.structured_payload)
        rendered_audit = f"{audit.human_summary} {audit.structured_payload}"

    assert notification is not None
    assert notification.body == raw_body
    assert chat_message.content == raw_body
    assert audit_payload["notification_id"] == result["notification_id"]
    assert audit.human_summary.startswith("clinical text redacted")
    assert raw_body not in rendered_audit


def test_eval_backlog_promotion_redacts_legacy_audit_summary(tmp_path, monkeypatch):
    from system_app.services import observability_actions

    raw_summary = "pytest legacy audit private peanut allergy summary"
    backlog_path = tmp_path / "agent_eval_backlog.json"
    monkeypatch.setattr(observability_actions, "EVAL_BACKLOG_PATH", backlog_path)

    with build_session() as session:
        audit = AgentDecisionAudit(
            trace_id="trace-legacy-audit-redaction",
            agent_name="legacy_agent",
            prompt_version_id="legacy",
            decision_type="legacy_tool_call",
            structured_payload="{}",
            human_summary=raw_summary,
            applied=False,
            error_message="",
            source_event_type="legacy",
        )
        session.add(audit)
        session.flush()
        audit_id = audit.id

        result = observability_actions.promote_observability_eval_case(
            session,
            source_type="audit",
            source_id=str(audit_id),
            reason="pytest legacy audit promotion",
        )

    cases = json.loads(backlog_path.read_text(encoding="utf-8"))
    rendered = json.dumps(cases, ensure_ascii=False)

    assert result["success"] is True
    assert result["created"] is True
    assert cases[0]["input"]["summary"].startswith("clinical text redacted")
    assert raw_summary not in rendered
    assert "pytest legacy audit private" not in rendered
