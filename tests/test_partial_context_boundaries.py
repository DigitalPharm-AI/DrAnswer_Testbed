from __future__ import annotations

from starlette.requests import Request

from system_app.db import SessionLocal
from system_app.services import dashboard_view
from system_app.services.clock_service import ensure_clock
from system_app.services.patient_profile_service import ensure_base_data


def _request(path: str = "/partials/test", query_string: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query_string,
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
    )


def test_polled_partial_contexts_do_not_run_full_dashboard_recovery_or_profile_queries(monkeypatch):
    with SessionLocal() as session:
        ensure_base_data(session)
        ensure_clock(session)

        def unexpected_full_dashboard_dependency(*_args, **_kwargs):
            raise AssertionError("polled partial invoked a full-dashboard-only dependency")

        monkeypatch.setattr(dashboard_view, "recover_expired_confirmations", unexpected_full_dashboard_dependency)
        monkeypatch.setattr(dashboard_view, "phr_profile_view", unexpected_full_dashboard_dependency)
        monkeypatch.setattr(dashboard_view, "simulation_readiness", unexpected_full_dashboard_dependency)

        contexts = {
            "timeline": dashboard_view.build_timeline_context(
                _request("/partials/timeline", b"timeline_date=2026-04-21"),
                session,
            ),
            "notifications": dashboard_view.build_notifications_context(
                _request("/partials/notifications"),
                session,
            ),
            "policies": dashboard_view.build_active_policies_context(
                _request("/partials/active-policies"),
                session,
            ),
            "nutrition": dashboard_view.build_nutrition_context(
                _request("/partials/nutrition"),
                session,
            ),
            "chat": dashboard_view.build_chat_context(_request("/partials/chat"), session),
            "chat_log": dashboard_view.build_chat_log_context(_request("/partials/chat-log"), session),
            "chat_history": dashboard_view.build_chat_history_context(
                _request("/partials/chat-history"),
                session,
            ),
            "system_history": dashboard_view.build_system_request_history_context(
                _request("/partials/system-request-history"),
                session,
            ),
        }

    assert set(contexts["timeline"]) == {
        "request",
        "clock",
        "dose_events",
        "selected_timeline_date",
        "timeline_date_param",
        "timeline_calendar",
    }
    assert contexts["timeline"]["selected_timeline_date"].isoformat() == "2026-04-21"
    assert set(contexts["notifications"]) == {
        "request",
        "notifications",
        "notification_metadata_map",
        "dose_status_map",
    }
    assert set(contexts["policies"]) == {
        "request",
        "active_policies",
        "system_policies",
        "reminder_suppressed_after_side_effect",
    }
    assert set(contexts["nutrition"]) == {"request", "nutrition"}
    assert set(contexts["chat"]) == {
        "request",
        "chat_messages",
        "conversation_history_messages",
        "active_chat_prompt",
        "active_ae_prompt",
        "mutation_execution_in_progress",
    }
    assert set(contexts["chat_log"]) == {"request", "chat_messages"}
    assert set(contexts["chat_history"]) == {"request", "conversation_history_messages"}
    assert set(contexts["system_history"]) == {"request", "system_request_history"}
