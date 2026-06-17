# Agent App Features

`agent_app`은 복약 서비스에서 LLM 판단, MCP tool 호출, async task queue 처리를 담당하는 FastAPI 서비스입니다. `system_app`은 사용자 상태와 UI를 관리하고, `agent_app`은 판단 결과를 동기 응답 또는 async callback으로 돌려줍니다.

## API Contract

| Method | Path | Request | Response | Mode |
| --- | --- | --- | --- | --- |
| GET | `/health` | 없음 | `{"status": "ok", "runtime": "langgraph_native"}` | public health |
| GET | `/agent/model-config` | 없음 | `AgentModelConfig` | sync |
| POST | `/agent/model-config` | `AgentModelTierRequest` | `AgentModelConfig` | sync |
| POST | `/agent/multiturn-chat` | `MultiturnChatRequest` | `AgentResponse` | sync |
| POST | `/agent/async/daily-patterns` | `DailyMedicationPattern` | `AgentAsyncAccepted` | async queue |
| POST | `/agent/async/missed-dose-events` | `MissedDoseEventPayload` | `AgentAsyncAccepted` | async queue |
| POST | `/agent/async/chat-continuations` | `MultiturnChatRequest` | `AgentAsyncAccepted` | async queue |
| POST | `/agent/async/push-messages` | `AgentAsyncPushMessageRequest` | `AgentAsyncAccepted` | async queue |
| POST | `/agent/async/clinician-alerts` | `AgentAsyncClinicianAlertRequest` | `AgentAsyncAccepted` | async queue |
| POST | `/agent/mcp` | JSON-RPC | JSON-RPC | internal MCP |
| GET | `/agent/async/tasks/status` | 없음 | queue counts + workers | ops |
| GET | `/agent/async/tasks` | `status`, `limit` query | task list | ops |
| GET | `/agent/async/tasks/dead` | `limit` query | dead task list | ops |
| GET | `/agent/async/tasks/{request_id}` | path id | task detail | ops |

Legacy sync endpoints for daily pattern and missed dose were removed:

- removed: `POST /agent/daily-patterns`
- removed: `POST /agent/missed-dose-events`

## Processing Model

- General multiturn chat remains synchronous through `/agent/multiturn-chat`.
- Daily pattern analysis and missed dose coaching are submitted through async queue endpoints.
- Chat continuations that need slow follow-up work, such as PRO-CTCAE or policy confirmation candidate generation, are submitted through `/agent/async/chat-continuations`.
- The standalone worker process claims pending async tasks from the agent DB and sends results back to `system_app` callback APIs.

## Internal Components

- `AgentLangGraphNativeOrchestrator`: routes request kinds to agent nodes.
- `DailyPatternAgent`: analyzes daily medication patterns and may propose notification policy tool calls.
- `MissedDoseAgent`: creates missed dose coaching output and checks side-effect signals.
- `MultiturnChatAgent`: handles immediate chat replies and decides whether an async continuation is needed.
- `ToolRuntime`: validates tool permissions, executes MCP tool calls, and records tool logs.
- `AgentMcpToolServer`: exposes internal tools through MCP-style JSON-RPC.
- `async_worker`: claims DB tasks, invokes the orchestrator, and posts callback results.

## Tool Policy

Tool calls are permission-checked before execution.

- `mark_dose_taken`: allowed only in multiturn chat when the dose event is present in context.
- `lookup_side_effect_info`: checks PHR side-effect information.
- `AE_pro_ctcae`: maps symptoms to PRO-CTCAE questions.
- `apply_notification_policy` and `apply_system_policy`: deferred policy tools. They create confirmation candidates instead of directly mutating policy.

## Observability

Agent trace logging writes structured `agent_api_call`, tool, worker, and validation events. The async task observability endpoints expose:

- task status and attempts
- lock owner and lock expiry
- task age and runtime seconds
- next retry timing
- payload keys only, not raw payload values
- callback context
- parse failure flags for malformed stored payloads

This lets operators debug stuck or dead tasks without exposing sensitive request values.
