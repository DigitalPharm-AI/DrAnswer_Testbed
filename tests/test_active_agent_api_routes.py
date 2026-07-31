from fastapi.testclient import TestClient

from agent_app.tools.mcp_server import AgentMcpToolServer
from shared.settings import get_settings
from system_app.db import SessionLocal
from system_app.main import app
from system_app.models import (
    BackendApiRequest,
    ChatMessage,
    Notification,
)
from system_app.services.patient_profile_service import ensure_base_data


def test_agent_domain_writes_have_no_direct_backend_surface_or_mcp_fallback():
    paths = set(app.openapi()["paths"])
    removed_direct_paths = {
        "/api/agent/mutation-confirmations/prepare",
        "/api/agent/dose-events/mark-taken",
        "/api/agent/nutrition/meals",
        "/api/agent/nutrition/meals/{meal_id}/update",
        "/api/agent/nutrition/meals/{meal_id}/delete",
        (
            "/api/agent/nutrition/meals/{meal_id}/"
            "foods/{food_id}/update"
        ),
        (
            "/api/agent/nutrition/meals/{meal_id}/"
            "foods/{food_id}/delete"
        ),
        "/api/agent/nutrition/preferences/facts",
    }
    removed_mcp_methods = {
        "_update_medication_dose_event_status",
        "_create_nutrition_meal_record",
        "_update_nutrition_meal_record",
        "_delete_nutrition_meal_record",
        "_update_nutrition_food_record",
        "_delete_nutrition_food_record",
        "_upsert_nutrition_preference_fact",
    }

    assert removed_direct_paths.isdisjoint(paths)
    assert "/agent/sync/record-change" in paths
    assert not any(
        hasattr(AgentMcpToolServer, method_name)
        for method_name in removed_mcp_methods
    )


def test_retired_direct_policy_apply_routes_are_removed():
    client = TestClient(app)

    notification_response = client.post(
        "/api/agent/policies/apply",
        json={},
    )
    system_response = client.post(
        "/api/agent/system-policies/apply",
        json={},
    )

    assert notification_response.status_code == 404
    assert system_response.status_code == 404
    assert "/api/agent/policies/apply" not in app.openapi()["paths"]
    assert "/api/agent/system-policies/apply" not in app.openapi()["paths"]


def test_agent_notification_callback_creates_notification_once_with_idempotency():
    client = TestClient(app)
    payload = {
        "idempotency_key": "test-notification-callback-key",
        "notification_type": "conversation_alert",
        "title": "부작용 확인 결과",
        "body": "부작용 가능성이 있어 추가 확인이 필요합니다.",
        "metadata": {"status": "agent_ready", "category": "side_effect"},
        "chat_category": "side_effect",
    }
    headers = {
        "X-Internal-Api-Token": get_settings().require_internal_api_token(),
    }

    with SessionLocal() as session:
        session.query(BackendApiRequest).filter(
            BackendApiRequest.api_path == "/api/agent/notifications",
            BackendApiRequest.request_id == payload["idempotency_key"],
        ).delete()
        session.query(ChatMessage).filter(
            ChatMessage.category == "side_effect"
        ).delete()
        session.query(Notification).filter(
            Notification.title == payload["title"]
        ).delete()
        ensure_base_data(session)
        session.commit()

    first_response = client.post(
        "/api/agent/notifications",
        json=payload,
        headers=headers,
    )
    second_response = client.post(
        "/api/agent/notifications",
        json=payload,
        headers=headers,
    )
    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text

    with SessionLocal() as session:
        notifications = (
            session.query(Notification)
            .filter(Notification.title == payload["title"])
            .all()
        )
        messages = (
            session.query(ChatMessage)
            .filter(ChatMessage.category == "side_effect")
            .all()
        )
        receipt = session.query(BackendApiRequest).filter(
            BackendApiRequest.api_path == "/api/agent/notifications",
            BackendApiRequest.request_id == payload["idempotency_key"],
        ).one()
        session.query(BackendApiRequest).filter(
            BackendApiRequest.api_path == "/api/agent/notifications",
            BackendApiRequest.request_id == payload["idempotency_key"],
        ).delete()
        session.query(ChatMessage).filter(
            ChatMessage.category == "side_effect"
        ).delete()
        session.query(Notification).filter(
            Notification.title == payload["title"]
        ).delete()
        session.commit()

    assert (
        first_response.json()["notification_id"]
        == second_response.json()["notification_id"]
    )
    assert len(notifications) == 1
    assert len(messages) == 1
    assert receipt.request_hash
    assert receipt.status == "COMPLETED"


def test_internal_agent_api_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_api_token", "test-internal-token")
    client = TestClient(app)

    blocked_response = client.post("/admin/policies/reload")
    allowed_response = client.post(
        "/admin/policies/reload",
        headers={"X-Internal-Api-Token": "test-internal-token"},
    )

    assert blocked_response.status_code == 401
    assert blocked_response.json()["detail"] == "invalid_internal_api_token"
    assert allowed_response.status_code == 200
