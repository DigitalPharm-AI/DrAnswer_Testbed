from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

UiTextPublisher = Callable[[str], Awaitable[None]]

_UI_TEXT_PUBLISHER: ContextVar[UiTextPublisher | None] = ContextVar(
    "backend_ui_text_publisher",
    default=None,
)


@contextmanager
def publish_ui_text_with(publisher: UiTextPublisher) -> Iterator[None]:
    token = _UI_TEXT_PUBLISHER.set(publisher)
    try:
        yield
    finally:
        _UI_TEXT_PUBLISHER.reset(token)


def ui_text_publisher() -> UiTextPublisher | None:
    return _UI_TEXT_PUBLISHER.get()
