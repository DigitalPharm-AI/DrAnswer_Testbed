from __future__ import annotations

from dataclasses import dataclass

from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.providers.base import BaseLLMProvider
from agent_app.providers.factory import create_llm_provider
from agent_app.tools.executor import McpAgentToolExecutor
from agent_app.tools.mcp_server import AgentMcpToolServer


@dataclass(frozen=True)
class AgentRuntimeComponents:
    provider: BaseLLMProvider
    tool_server: AgentMcpToolServer
    tool_executor: McpAgentToolExecutor
    orchestrator: AgentLangGraphNativeOrchestrator


def create_mcp_tool_server() -> AgentMcpToolServer:
    return AgentMcpToolServer()


def create_tool_executor(tool_server: AgentMcpToolServer | None = None) -> McpAgentToolExecutor:
    return McpAgentToolExecutor(server=tool_server or create_mcp_tool_server())


def create_orchestrator() -> AgentLangGraphNativeOrchestrator:
    return create_runtime_components().orchestrator


def create_runtime_components() -> AgentRuntimeComponents:
    provider = create_llm_provider()
    tool_server = create_mcp_tool_server()
    tool_executor = create_tool_executor(tool_server)
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor)
    return AgentRuntimeComponents(
        provider=provider,
        tool_server=tool_server,
        tool_executor=tool_executor,
        orchestrator=orchestrator,
    )
