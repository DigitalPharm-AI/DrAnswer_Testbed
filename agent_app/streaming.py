from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

AgentTextPublisher = Callable[[str], Awaitable[None]]

_AGENT_TEXT_PUBLISHER: ContextVar[AgentTextPublisher | None] = (
    ContextVar(
        "agent_public_text_publisher",
        default=None,
    )
)


@contextmanager
def publish_agent_text_with(
    publisher: AgentTextPublisher,
) -> Iterator[None]:
    token = _AGENT_TEXT_PUBLISHER.set(publisher)
    try:
        yield
    finally:
        _AGENT_TEXT_PUBLISHER.reset(token)


def agent_text_publisher() -> AgentTextPublisher | None:
    return _AGENT_TEXT_PUBLISHER.get()
