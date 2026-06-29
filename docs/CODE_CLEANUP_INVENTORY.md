# Code Cleanup Inventory

Generated from the current DA_drug agent/app integration work.

## P0 cleanup targets

| Target | Duplicate / unnecessary shape | Cleanup action | Safety check |
|---|---|---|---|
| Trace retention | `TRACE_RETENTION_POLICY` and expired trace selection live in both `observability_view.py` and `observability_actions.py`. | Move policy, expired trace query, and cleanup action to `system_app.services.trace_retention`. | `tests/test_agent_server_structure.py`, `tests/test_ui_pages.py::test_logs_trace_retention_cleanup_records_audit_and_redacts_old_spans` |
| Tool permission gate | `ToolRuntime` and `AgentMcpToolServer` both validate permission and deferred policy behavior. | Make `AgentMcpToolServer` the authoritative execution/permission boundary; keep `ToolRuntime` as plan logging + executor delegation. | `tests/test_agent_server_structure.py::test_mcp_server_is_authoritative_tool_permission_gate` |
| 9000 runtime settings | Runtime can be launched on 9000/9001/9002 while defaults still point to 8000/8001/8002. | Added `.env.9000.example` so app/agent/phr bases align. | LOGS async status should show 9001 agent when launched with this env. |

## P1 legacy candidates to keep under review

| Target | Current reason to keep | Future removal condition |
|---|---|---|
| `/agent/multiturn-chat` sync API | Existing regression tests and fallback paths still reference `send_multiturn_chat`. | Remove only after chat/system workers and tests rely exclusively on `/agent/async/chat-continuations`. |
| `system_event_worker` sync fallback branch | Backward-compatible branch if `AgentClient` lacks `send_chat_continuation_async`. | Remove when compatibility with older AgentClient stubs is no longer needed. |
| `/api/agent/policies/apply` and `/api/agent/system-policies/apply` 410 routes | Tests verify direct policy mutation is blocked with an explicit 410. | Remove only if all legacy clients have moved to deferred policy confirmation. |
| Dual queues: `AgentJob` and `AgentAsyncTask` | They are not the same queue: `AgentJob` is a system_app bridge queue; `AgentAsyncTask` is agent execution queue. | Revisit only if service boundary is collapsed or a shared broker is introduced. |

## Usage check notes

- `/agent/multiturn-chat` is still referenced by agent regression tests, older system worker fallback paths, and tooling diagrams.
- `send_multiturn_chat` is still used by tests and compatibility stubs, so it is not removed in this cleanup pass.
- The direct policy mutation APIs intentionally return `410` and are still asserted by tests and Playwright regression tooling.
- `AgentJob` and `AgentAsyncTask` are both retained because they represent different service-boundary queues.

## Current structure tests

- `tests/test_agent_server_structure.py` fixes the agent API route contract.
- It also checks `ToolCatalog` names match the protocol allowlist and HITL/deferred policy flags.
- It locks the intended permission structure: `ToolRuntime` delegates, `AgentMcpToolServer` decides.
