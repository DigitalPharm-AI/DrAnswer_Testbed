from __future__ import annotations

import os
from pathlib import Path

TEST_DATA_DIR = Path("data") / "pytest_runtime"
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

os.environ["SYSTEM_DATABASE_URL"] = f"sqlite:///{TEST_DATA_DIR / 'system.db'}"
os.environ["AGENT_DATABASE_URL"] = f"sqlite:///{TEST_DATA_DIR / 'agent.db'}"
os.environ["PHR_DATABASE_URL"] = f"sqlite:///{TEST_DATA_DIR / 'phr.db'}"
os.environ["PROMPT_WORKBOOK_PATH"] = str(TEST_DATA_DIR / "prompt_registry.xlsx")
os.environ["POLICY_WORKBOOK_PATH"] = str(TEST_DATA_DIR / "default_notification_policies.xlsx")

from phr_app.db import engine as phr_engine  # noqa: E402
from phr_app.migrations import run_migrations as run_phr_migrations  # noqa: E402
from phr_app.models import Base as PhrBase  # noqa: E402
from agent_app.db import engine as agent_engine  # noqa: E402
from agent_app.migrations import run_migrations as run_agent_migrations  # noqa: E402
from agent_app.models import Base as AgentBase  # noqa: E402
from system_app.db import engine as system_engine  # noqa: E402
from system_app.migrations import run_migrations as run_system_migrations  # noqa: E402
from system_app.models import Base as SystemBase  # noqa: E402

SystemBase.metadata.create_all(bind=system_engine)
run_system_migrations(system_engine)
AgentBase.metadata.create_all(bind=agent_engine)
run_agent_migrations(agent_engine)
PhrBase.metadata.create_all(bind=phr_engine)
run_phr_migrations(phr_engine)
