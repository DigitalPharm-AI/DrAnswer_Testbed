# Code Cleanup Inventory

Generated from the current DA_drug agent/app integration work.

## 2026-07-29 completed cleanup

- Removed unreferenced Backend route/response, governance, release-readiness,
  system trace, legacy diet recommendation, simulation facade, and conversation
  facade modules.
- Removed the unused direct mutation executor/finalizer. Confirmations now have
  one write boundary: the AI Server prepares and tracks approval state, while
  the Backend v1.3 API performs the authoritative database mutation.
- Removed unused Agent scheduler/query remnants, DTOs, settings, compatibility
  wrappers, old generated-browser helpers, and stale observability artifacts.
- Consolidated the three identical model-output normalizers, MCP error
  sanitization, expected-version extraction, and React feedback/reaction
  contracts.
- Removed import-time environment mutation from all OpenAPI exporters, making
  schema checks independent of test order and process-global settings.
- Removed unused React API methods, exported helpers, and CSS selectors.
- Migrated the Contract Test Server's final SQLite state into its own
  PostgreSQL database and removed every repository SQLite DB/WAL/SHM file,
  importer, runtime fallback, and `sqlite3` import.
- Merged 31 legacy Backend Trace rows and 86 Step rows into the Agent DB, then
  removed Backend Trace tables, writers, metadata keys, and generated evidence.
- Removed retired `conversation_id` columns and database object names. Removed
  the obsolete Trace `fallback_reason` column in favor of
  `final_answer_source`.
- Removed legacy PHR/UI artifacts, old browser traces, past contract evidence,
  compatibility shims, and historical documents that described deleted
  runtime paths.
- Recreated the disposable System/Agent PostgreSQL test databases without
  their stale `public` schemas and removed every leaked `browser_ci_*` schema.
  The browser runner now uses only its generated schema in `search_path`, so
  SQLAlchemy cannot mistake live `public` tables for isolated test tables.
- Changed the browser runner to stream Uvicorn output to a temporary log file
  instead of an unread pipe. Startup exceptions are now reported immediately
  and cannot be disguised as readiness timeouts.
- Consolidated the scripted test model under the explicit
  `deterministic_test` provider and removed the standalone rule-based provider
  aliases, obsolete 9000 profile, readiness exception, and historical
  fallback-drill artifact. Bedrock failures remain fail-closed.
- Treats `output/` and `system_app/static/react/` as generated artifacts.
  The local stack launcher builds React before service startup.
- Keeps PostgreSQL migration tooling, deterministic test providers, active
  v1.3 endpoints, and external-callback candidates because they still have
  operational or contract value.

## P0 cleanup targets

| Target | Duplicate / unnecessary shape | Cleanup action | Safety check |
|---|---|---|---|
| Trace ownership/retention | 완료: Backend observability Trace 복제 경로를 제거하고 Agent DB를 단일 원장으로 사용한다. | `shared.retention_policy`의 3년 정책과 Agent `expires_at` cleanup을 유지하고, Backend에는 최소 API 감사만 남긴다. | `tests/test_agent_trace_store.py`, `tests/test_backend_trace_ownership.py`, `tests/test_backend_audit_retention.py` |
| Tool permission gate | `ToolRuntime` and `AgentMcpToolServer` both validate permission and deferred policy behavior. | Make `AgentMcpToolServer` the authoritative execution/permission boundary; keep `ToolRuntime` as plan logging + executor delegation. | `tests/test_agent_server_structure.py::test_mcp_server_is_authoritative_tool_permission_gate` |

## Current queue boundary

`AgentJob`과 `AgentAsyncTask`는 중복 구현이 아니다. 전자는 Backend의
비동기 접수·Callback bridge이고 후자는 Agent 실행 queue이므로 서비스
경계를 합치거나 공용 broker를 도입하기 전까지 둘 다 유지한다.

## Usage check notes

- The former standalone PHR package, database settings, registration client,
  schemas, and compatibility tests were removed after a repository-wide usage
  check. Backend Patient Snapshot remains the patient-data source of truth.

- `/agent/async/chat-continuations` and its worker/callback path were removed;
  user-facing chat now uses only `/agent/sync/chat`.
- `/agent/multiturn-chat` and `/agent/mutation-confirmations/resolve` were removed;
  graph regression tests now invoke the orchestrator directly and v1.3 write
  tools call the Backend write contract synchronously.
- The direct `/api/agent/dose-events/mark-taken` and
  `/api/agent/nutrition/*` write routes, plus their unreachable MCP forwarding
  methods, were removed. Medication, side-effect, and meal/food mutations now
  use `/agent/sync/record-change`; notification policy mutations use
  `/agent/sync/notification-policy-change`.
- 직접 정책 mutation API는 route 자체가 제거되었으며, 현재 테스트는 해당
  경로가 OpenAPI와 runtime 모두에 없음을 검증한다.
- `AgentJob` and `AgentAsyncTask` are both retained because they represent different service-boundary queues.

## Current structure tests

- `tests/test_agent_server_structure.py` fixes the agent API route contract.
- It also checks `ToolCatalog` names match the protocol allowlist and HITL/deferred policy flags.
- It locks the intended permission structure: `ToolRuntime` delegates, `AgentMcpToolServer` decides.
