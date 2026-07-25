from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_app.jobs.tasks import DONE, enqueue_async_task
from agent_app.jobs.worker import async_task_worker
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.persistence.migrations import run_migrations
from agent_app.persistence.models import AgentAsyncTask, Base
from agent_app.tools.executor import McpAgentToolExecutor
from agent_app.tools.mcp_server import AgentMcpToolServer
from shared.db import create_session_factory
from tests.support.llm import NativeChatProvider

POSTGRES_TEST_DATABASE_URL = os.getenv("AGENT_POSTGRES_TEST_DATABASE_URL")


class AeToolProvider(NativeChatProvider):
    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "message": "증상 문항을 준비합니다.",
            "tool_call": {
                "name": "get_pro_ctcae_questionnaire",
                "arguments": {
                    "symptom_text": "속이 메스꺼워요",
                    "symptom_normalize": "메스꺼움",
                },
            },
        }


class CallbackRecorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.event = threading.Event()


def _start_callback_server(recorder: CallbackRecorder) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length)
            recorder.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "json": json.loads(body.decode("utf-8")),
                }
            )
            recorder.event.set()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


@pytest.mark.skipif(not POSTGRES_TEST_DATABASE_URL, reason="AGENT_POSTGRES_TEST_DATABASE_URL is not set")
def test_postgres_mcp_async_worker_chat_continuation_callback_round_trip(monkeypatch):
    engine, SessionLocal = create_session_factory(POSTGRES_TEST_DATABASE_URL)
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    monkeypatch.setattr("agent_app.jobs.worker.SessionLocal", SessionLocal)

    recorder = CallbackRecorder()
    callback_server, callback_base_url = _start_callback_server(recorder)
    request_id = f"pytest-postgres-mcp-{uuid.uuid4().hex}"
    stop_event = threading.Event()
    orchestrator = AgentLangGraphNativeOrchestrator(
        provider=AeToolProvider(),
        tool_executor=McpAgentToolExecutor(server=AgentMcpToolServer()),
    )

    try:
        with SessionLocal() as session:
            enqueue_async_task(
                session,
                request_id=request_id,
                task_type="chat_continuation",
                payload={
                    "patient_id": "demo-patient",
                    "event_type": "multiturn_chat",
                    "message": "속이 메스꺼워요",
                    "current_time": "2026-04-20T09:35:00",
                    "context": {},
                    "callback_context": {
                        "app_base_url": callback_base_url,
                        "notification_id": 12345,
                    },
                },
                callback_context={
                    "app_base_url": callback_base_url,
                    "notification_id": 12345,
                },
                max_attempts=1,
            )
            session.commit()

        worker = threading.Thread(target=async_task_worker, args=(stop_event, orchestrator), daemon=True)
        worker.start()
        assert recorder.event.wait(20), "async worker did not send callback"

        with SessionLocal() as session:
            task = session.query(AgentAsyncTask).filter(AgentAsyncTask.request_id == request_id).one()
            assert task.status == DONE

        callback = recorder.requests[0]
        assert callback["path"] == "/api/agent/async/chat-results"
        assert callback["json"]["request_id"] == request_id
        structured = callback["json"]["response"]["structured_payload"]
        assert structured["tool_call"]["name"] == "get_pro_ctcae_questionnaire"
        assert structured["tool_results"][0]["tool_name"] == "get_pro_ctcae_questionnaire"
        assert structured["tool_results"][0]["status"] == "success"
        assert structured["ae_pro_ctcae"]["matched"] is True
    finally:
        stop_event.set()
        time.sleep(0.5)
        callback_server.shutdown()
        callback_server.server_close()
        with SessionLocal() as session:
            session.query(AgentAsyncTask).filter(AgentAsyncTask.request_id == request_id).delete()
            session.commit()
