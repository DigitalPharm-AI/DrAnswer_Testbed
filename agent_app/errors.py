from __future__ import annotations


class AgentExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        trace_id: str,
        agent_name: str,
        decision_type: str,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.trace_id = trace_id
        self.agent_name = agent_name
        self.decision_type = decision_type
