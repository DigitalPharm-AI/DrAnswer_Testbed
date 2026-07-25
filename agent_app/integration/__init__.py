"""Backend integration boundary for the external AI module contract."""

from agent_app.integration.backend_client import BackendV12Client
from agent_app.integration.chat_contracts import ChatSyncRequest, ChatSyncResponse
from agent_app.integration.contracts import (
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    RecordChangeRequest,
    RecordChangeResponse,
)

__all__ = [
    "BackendV12Client",
    "ChatSyncRequest",
    "ChatSyncResponse",
    "NotificationPolicyChangeRequest",
    "NotificationPolicyChangeResponse",
    "RecordChangeRequest",
    "RecordChangeResponse",
]
