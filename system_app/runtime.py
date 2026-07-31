from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SystemRuntime:
    write_lock: threading.RLock
    agent_client: Any
