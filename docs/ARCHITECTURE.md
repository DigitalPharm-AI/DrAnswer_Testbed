# Architecture

## Services

- `system_app`
  - Owns medication schedules, dose events, notifications, chat history, policy confirmations, and the user-facing UI.
  - Submits long-running agent work asynchronously.
  - Receives agent callbacks and persists chat messages, policy candidates, push notifications, and failure records.

- `agent_app`
  - Owns LLM orchestration, deterministic rule fallback, MCP tool execution, and the agent async task queue.
  - Keeps normal multiturn chat synchronous.
  - Runs daily pattern, missed dose, push message, clinician alert, and chat continuation work through DB-backed async tasks.

- `phr_app`
  - Owns PHR-side medication precaution data.
  - Serves side-effect assessment requests from the `lookup_side_effect_info` MCP tool.

## Agent App Request Flow

```mermaid
flowchart LR
    system["system_app"] -->|"POST /agent/async/daily-patterns"| agentQueue["agent async queue"]
    system -->|"POST /agent/async/missed-dose-events"| agentQueue
    system -->|"POST /agent/async/chat-continuations"| agentQueue
    system -->|"POST /agent/multiturn-chat"| chat["MultiturnChatAgent"]
    agentQueue --> worker["agent worker"]
    worker --> graph["LangGraph orchestrator"]
    graph --> daily["DailyPatternAgent"]
    graph --> missed["MissedDoseAgent"]
    graph --> continuation["MultiturnChatAgent continuation"]
    daily --> callback["system_app async callbacks"]
    missed --> callback
    continuation --> callback
    chat -->|"immediate AgentResponse"| system
```

## Sync vs Async

- Synchronous:
  - `GET /health`
  - `GET/POST /agent/model-config`
  - `POST /agent/multiturn-chat`
  - `POST /agent/mcp`

- Asynchronous:
  - `POST /agent/async/daily-patterns`
  - `POST /agent/async/missed-dose-events`
  - `POST /agent/async/chat-continuations`
  - `POST /agent/async/push-messages`
  - `POST /agent/async/clinician-alerts`

Legacy daily/missed sync endpoints have been removed. Any call to `/agent/daily-patterns` or `/agent/missed-dose-events` should now fail with `404`.

## Worker Model

The production worker is a plain Python process, started separately from FastAPI:

```powershell
python -m agent_app.worker_main
```

It uses the agent DB queue:

1. claim one pending task
2. mark it running and lock it
3. invoke the LangGraph orchestrator or push/clinician task handler
4. send callback to `system_app`
5. mark task done, retry, or dead

The embedded FastAPI worker remains disabled by default and is intended only for local development.

## Tools

Agent tool calls go through MCP-style JSON-RPC internally:

- `McpAgentToolExecutor`
- `AgentMcpToolServer`
- `ToolRuntime`

Tools are allowlisted and permission-checked before execution. Policy tools are deferred and produce confirmation candidates rather than applying changes directly.

## Operations

Async task status is visible through:

- `/agent/async/tasks/status`
- `/agent/async/tasks`
- `/agent/async/tasks/dead`
- `/agent/async/tasks/{request_id}`

These endpoints expose timing, lock, retry, and error metadata while hiding raw task payload values.
