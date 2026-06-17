from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from fastapi.templating import Jinja2Templates


@dataclass(frozen=True)
class SystemRuntime:
    templates: Jinja2Templates
    write_lock: threading.RLock
    agent_client: Any
    phr_client: Any
    system_event_worker: Any
