# Agent App Features

`agent_app`은 복약 서비스에서 LLM 판단, MCP Tool 호출, async task queue
처리를 담당하는 FastAPI 서비스입니다. 사용자 채팅과 Backend 업무 쓰기는
연동규격 v1.3을 따릅니다. 외부 비동기 접수는 미복용 이벤트와 Backend가
매일 02:00에 시작하는 일일 패턴 분석만 허용합니다.

## API Contract

| Method | Path | Request | Response | Mode |
| --- | --- | --- | --- | --- |
| GET | `/health` | 없음 | `{"status": "ok", "runtime": "langgraph_native"}` | public health |
| GET | `/agent/model-config` | 없음 | `AgentModelConfig` | sync |
| POST | `/agent/model-config` | `AgentModelTierRequest` | `AgentModelConfig` | sync |
| POST | `/agent/sync/chat` | `ChatSyncRequest` | LF-delimited `ChatStreamEvent` (`application/x-ndjson`) | sync stream |
| POST | `/agent/async/chat_feedback` | `ChatFeedbackRequest` | `ChatFeedbackAccepted` | async feedback |
| POST | `/agent/async/missed-dose-events` | `MissedDoseEventRequest` | `AsyncEventAccepted` | async queue |
| POST | `/agent/async/daily-medication-pattern-analysis` | `DailyMedicationPatternAnalysisRequest` | `AsyncEventAccepted` | async queue |
| POST | `/agent/async/push-messages` | `AgentAsyncPushMessageRequest` | internal task acceptance | async queue |
| POST | `/agent/async/clinician-alerts` | `AgentAsyncClinicianAlertRequest` | internal task acceptance | async queue |
| POST | `/agent/mcp` | JSON-RPC | JSON-RPC | internal MCP |
| GET | `/agent/ops/readiness` | 없음 | readiness metrics + alerts | ops |
| GET | `/agent/async/tasks/status` | 없음 | queue counts + workers | ops |
| GET | `/agent/async/tasks` | `status`, `limit` query | task list | ops |
| GET | `/agent/async/tasks/dead` | `limit` query | dead task list | ops |
| GET | `/agent/async/tasks/{request_id}` | path id | task detail | ops |

Legacy raw chat, confirmation resolution, and sync background endpoints were removed:

- removed: `POST /agent/multiturn-chat`
- removed: `POST /agent/mutation-confirmations/resolve`
- removed: `POST /agent/daily-patterns`
- removed: `POST /agent/missed-dose-events`
- removed: `POST /agent/async/daily-patterns`
- removed: `POST /agent/sync/chat/stream`

## Processing Model

- The user-facing chat flow uses only `/agent/sync/chat`. The Backend sends
  `Accept: application/x-ndjson`; no second streaming route exists.
- Every NDJSON line is a complete `ChatStreamEvent`. `sequence` starts at 0 and
  increments by one, and the response ends with exactly one `completed` or
  `error` event.
- Before the LLM runs, the AI Server verifies the persisted Backend message and
  loads the current patient Snapshot from approved Backend DB Views through a
  Read-only connection.
- The Snapshot contains the minimum profile, active conditions/treatments,
  today's medication schedules and dose events, today's meals, and currently
  effective notification policies.
- The model receives an LLM-safe projection. Tool Runtime reuses the trusted
  Snapshot and injects patient scope, record IDs, and versions.
- Agent graph regressions invoke the orchestrator directly instead of exposing
  a raw `AgentResponse` HTTP endpoint.
- When a Tool continuation is required, the Agent completes it inside the same
  request, trace, and overall timeout. Intermediate control responses have an
  empty `human_summary` and are never returned as a successful v1.3 response.
- Backend→AI Server 미복용 요청은 `request_id`, `patient_id`,
  `dose_event_id` 세 필드만 전송한다. Worker가 승인된 Read-only View에서
  대상 이벤트, 현재 환자 문맥, Backend가 판정해 저장한 A~E 복약 패턴과
  톤 정책 문맥을 조회한다.
- 미복용 성공·실패 결과는
  `/api/agent/async/missed-dose-results` 전용 Callback으로 전송한다.
- Backend owns the daily 02:00 trigger and submits the patient batch to
  `/agent/async/daily-medication-pattern-analysis`. AI Server owns only the
  accepted analysis and proposal-delivery tasks. A Backend callback is made
  only when analysis produces a notification-policy proposal, using
  `/api/agent/async/notification-policy-change-proposals`.
- Generic worker failures are retained in the AI Internal DB task/Trace ledger;
  they are not copied to a Backend failure endpoint.
- The standalone worker processes non-chat background work: missed-dose
  analysis, daily-pattern analysis/proposal delivery, and internal
  push/clinician tasks.

## Internal Components

- `AgentLangGraphNativeOrchestrator`: routes request kinds to agent nodes.
- `DailyPatternAgent`: analyzes daily medication patterns and may propose notification policy tool calls.
- `MissedDoseAgent`: validates the Backend-selected A~E category and tone, then
  makes one Tool-free LLM call for a strict, short Korean message. Invalid
  output is returned as an explicit error without a predefined-message
  fallback.
- `MultiturnChatAgent`: handles chat replies and marks internal Tool continuation control when needed.
- `ToolRuntime`: validates tool permissions, executes MCP tool calls, and records tool logs.
- `AgentMcpToolServer`: exposes internal tools through MCP-style JSON-RPC.
- `async_worker`: claims DB tasks, invokes the orchestrator, and posts callback results.

미복용 감지 시점에는 환자가 직접 입력한 증상이 없으므로 부작용 조회와
PRO-CTCAE Tool을 실행하지 않는다. 환자가 미복용 알림에 답한 뒤의 동기
채팅에서는 `MultiturnChatAgent`가 `MedicationAgent`로 위임하고, 명시된
증상이 있을 때만 부작용·PRO-CTCAE Tool 흐름을 사용한다.

Backend는 Callback 직후 현재 복용 상태를 다시 확인한다. 상태가 여전히
`missed`일 때만 정책 재판정과 LLM 문구 안전 검증을 거쳐 알림·채팅으로
전달하며, 이미 복용한 상태라면 대기 알림을 폐기하고 채팅을 생성하지
않는다.

## Tool Policy

Tool calls are permission-checked before execution.

- `update_medication_dose_event_status`: uses the trusted patient scope and
  Backend record version; the model cannot supply technical identifiers.
- `get_medication_side_effect_assessment`: reuses the current patient Snapshot,
  matches medication candidates, and uses non-patient medication reference data.
- `get_pro_ctcae_questionnaire`: maps an assessed symptom to PRO-CTCAE questions.
- Side-effect records and other business records are persisted through Backend
  v1.3 synchronous write APIs.
- Policy proposals and changes retain their required confirmation and Backend
  validation boundaries.

Detailed historical reads are bounded Tools:

- past medication history
- complete side-effect history
- past meal history

The active v1.3 flow uses the Backend Patient Snapshot. The former standalone
PHR registration service, database, patient key, settings, and compatibility
client have been removed.

## Data Ownership

| Data | Owner | AI Server access |
| --- | --- | --- |
| Patient, medication, meal, side-effect, chat, and policy data | Backend DB | Approved Views, Read-only |
| Business create/update/delete | Backend Server | Synchronous authenticated API |
| Sync/async/failed Trace, token/cost, Step, Tool state, idempotency, async queue | AI Internal DB (sole Trace ledger) | Read/write |
| Medication adverse-effect and food nutrition reference data | Reference repository | Tool-only lookup |

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

Trace, failed Trace, Step, Tool execution, token usage, and cost evidence use
one fixed 3-year retention window (1095 days). `expires_at` drives deletion;
patient identifiers are hashed and clinical free text is redacted before
storage. The Backend does not store a second copy of these Trace rows.

`/agent/ops/readiness` summarizes the same queue and worker state as alert-oriented readiness:

- `agent_async_worker_unavailable`
- `agent_async_dead_tasks_present`
- `agent_async_callback_failures_detected`
- `agent_provider_failures_detected`
- `agent_async_worker_stale`
- `agent_async_pending_age_exceeded`

Incident handling, rollback, and redaction policy are maintained in `docs/PRODUCTION_READINESS.md`.
