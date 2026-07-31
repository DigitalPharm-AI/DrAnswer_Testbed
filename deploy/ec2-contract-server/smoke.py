from __future__ import annotations

import json
import secrets
from pathlib import Path

import httpx

from shared.async_v13_contracts import (
    MissedDoseResultCallback,
    NotificationPolicyChangeProposalRequest,
)
from shared.chat_contracts import ChatStreamEvent

ENV_FILE = Path("/etc/dranswer-agent-contract/contract.env")
RELEASE_ENV_FILE = Path("/etc/dranswer-agent-contract/release.env")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def require_status(response: httpx.Response, expected: int) -> None:
    if response.status_code != expected:
        raise AssertionError(
            f"unexpected_http_status:{response.request.url.path}:"
            f"{response.status_code}:{expected}"
        )


def main() -> None:
    env = {**read_env(ENV_FILE), **read_env(RELEASE_ENV_FILE)}
    base_url = f"http://127.0.0.1:{env.get('CONTRACT_PORT', '8701')}"
    agent_headers = {
        "Authorization": f"Bearer {env['AGENT_SYNC_API_TOKEN']}",
    }
    control_headers = {
        "X-Test-Control-Token": env["TEST_CONTROL_TOKEN"],
    }
    patient_ids = [public_id("patient"), public_id("patient")]

    with httpx.Client(
        base_url=base_url,
        timeout=5,
        follow_redirects=False,
    ) as client:
        health = client.get("/health")
        ready = client.get("/health/ready")
        require_status(health, 200)
        require_status(ready, 200)
        assert health.json()["release"] == env["APP_RELEASE_VERSION"]
        assert ready.json()["callback_mode"] == "hold"
        assert ready.json()["callback_delivery_ready"] is False

        unauthorized = client.post(
            "/agent/async/missed-dose-events",
            json={
                "request_id": public_id("req"),
                "patient_id": patient_ids[0],
                "dose_event_id": public_id("dose"),
            },
        )
        require_status(unauthorized, 401)
        assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"

        chat_counts: dict[str, tuple[int, int]] = {}
        for return_type in ("text", "selection_box", "input_box"):
            request_id = public_id("req")
            payload = {
                "request_id": request_id,
                "message_id": public_id("user_msg"),
                "patient_id": patient_ids[0],
                "requested_return_type": return_type,
                "message": (
                    '{"value":1}'
                    if return_type == "input_box"
                    else "contract smoke"
                ),
                "message_at": "2026-07-29T23:30:00+09:00",
            }
            headers = {
                **agent_headers,
                "Accept": "application/x-ndjson",
            }
            first = client.post(
                "/agent/sync/chat",
                json=payload,
                headers=headers,
            )
            replay = client.post(
                "/agent/sync/chat",
                json=payload,
                headers=headers,
            )
            require_status(first, 200)
            require_status(replay, 200)
            assert first.headers["content-type"].startswith(
                "application/x-ndjson"
            )
            first_rows = [
                json.loads(line)
                for line in first.content.splitlines()
                if line
            ]
            replay_rows = [
                json.loads(line)
                for line in replay.content.splitlines()
                if line
            ]
            first_events = [
                ChatStreamEvent.model_validate(row)
                for row in first_rows
            ]
            replay_events = [
                ChatStreamEvent.model_validate(row)
                for row in replay_rows
            ]
            assert [event.sequence for event in first_events] == [0, 1]
            assert first_events[-1].message_type == return_type
            assert len(replay_events) == 1
            assert replay_events[0].sequence == 0
            assert replay_events[0].status == "completed"
            chat_counts[return_type] = (
                len(first_events),
                len(replay_events),
            )

        feedback_payload = {
            "request_id": public_id("req"),
            "message_id": public_id("assistant_msg"),
            "patient_id": patient_ids[0],
            "reaction": "like",
            "feedback_text": "synthetic contract smoke feedback",
            "feedback_at": "2026-07-29T23:31:00+09:00",
        }
        feedback = client.post(
            "/agent/async/chat_feedback",
            json=feedback_payload,
            headers=agent_headers,
        )
        feedback_replay = client.post(
            "/agent/async/chat_feedback",
            json=feedback_payload,
            headers=agent_headers,
        )
        require_status(feedback, 202)
        require_status(feedback_replay, 202)
        assert feedback.json() == feedback_replay.json()

        missed_request_id = public_id("req")
        missed_payload = {
            "request_id": missed_request_id,
            "patient_id": patient_ids[0],
            "dose_event_id": public_id("dose"),
        }
        missed = client.post(
            "/agent/async/missed-dose-events",
            json=missed_payload,
            headers=agent_headers,
        )
        missed_replay = client.post(
            "/agent/async/missed-dose-events",
            json=missed_payload,
            headers=agent_headers,
        )
        require_status(missed, 202)
        require_status(missed_replay, 200)
        assert missed_replay.json()["status"] == "duplicate"

        daily_request_id = public_id("req")
        daily_payload = {
            "request_id": daily_request_id,
            "patient_id": patient_ids,
            "analysis_date": "2026-07-29",
        }
        daily = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json=daily_payload,
            headers=agent_headers,
        )
        daily_replay = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json=daily_payload,
            headers=agent_headers,
        )
        require_status(daily, 202)
        require_status(daily_replay, 200)
        assert daily_replay.json()["status"] == "duplicate"

        callback_response = client.get(
            "/_test/callbacks?limit=100",
            headers=control_headers,
        )
        require_status(callback_response, 200)
        callbacks = [
            value
            for value in callback_response.json()["callbacks"]
            if value["source_request_id"]
            in {missed_request_id, daily_request_id}
        ]
        assert len(callbacks) == 3
        assert all(value["status"] == "held" for value in callbacks)
        assert all(value["attempts"] == 0 for value in callbacks)
        for value in callbacks:
            if value["callback_kind"] == "missed_dose_result":
                MissedDoseResultCallback.model_validate(value["payload"])
            else:
                NotificationPolicyChangeProposalRequest.model_validate(
                    value["payload"]
                )

        for artifact in (
            "chat.json",
            "async-medication.json",
            "backend-callbacks.json",
        ):
            require_status(
                client.get(f"/contracts/v1.3/{artifact}"),
                200,
            )

    print(
        json.dumps(
            {
                "release": env["APP_RELEASE_VERSION"],
                "health": "ok",
                "ready": "ready",
                "chat_event_counts": chat_counts,
                "feedback": "accepted_and_replayed",
                "missed_dose": "accepted_and_duplicate",
                "daily_analysis": "accepted_and_duplicate",
                "held_callbacks": len(callbacks),
                "callback_attempts": 0,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
