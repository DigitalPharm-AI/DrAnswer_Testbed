from __future__ import annotations

import ast
from pathlib import Path

from agent_app.integration.chat_contracts import ChatSyncRequest as AgentChatSyncRequest
from agent_app.integration.contracts import RecordChangeRequest as AgentRecordChangeRequest
from agent_app.orchestration.confirmations import ConfirmationActionRegistry as AgentConfirmationActionRegistry
from agent_app.tools.catalog import ToolCatalog as AgentToolCatalog
from agent_app.tools.names import GET_MEDICATION_DOSE_STATUS as AGENT_GET_MEDICATION_DOSE_STATUS
from agent_app.tools.permissions import validate_tool_permission as agent_validate_tool_permission
from shared.backend_v12_contracts import RecordChangeRequest
from shared.chat_contracts import ChatSyncRequest
from shared.tool_catalog import ToolCatalog
from shared.tool_confirmations import ConfirmationActionRegistry
from shared.tool_names import GET_MEDICATION_DOSE_STATUS
from shared.tool_permissions import validate_tool_permission

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_system_app_has_no_static_agent_app_imports() -> None:
    violations = _imports_from(REPO_ROOT / "system_app", forbidden_roots={"agent_app"})

    assert violations == []


def test_shared_contracts_do_not_depend_on_application_packages() -> None:
    violations = _imports_from(
        REPO_ROOT / "shared",
        forbidden_roots={"agent_app", "system_app"},
    )

    assert violations == []


def test_agent_compatibility_modules_reexport_shared_objects() -> None:
    assert AgentChatSyncRequest is ChatSyncRequest
    assert AgentRecordChangeRequest is RecordChangeRequest
    assert AgentConfirmationActionRegistry is ConfirmationActionRegistry
    assert AgentToolCatalog is ToolCatalog
    assert AGENT_GET_MEDICATION_DOSE_STATUS == GET_MEDICATION_DOSE_STATUS
    assert agent_validate_tool_permission is validate_tool_permission


def _imports_from(root: Path, *, forbidden_roots: set[str]) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules = [node.module]
            for module in imported_modules:
                package_root = module.split(".", maxsplit=1)[0]
                if package_root in forbidden_roots:
                    relative_path = path.relative_to(REPO_ROOT).as_posix()
                    violations.append(f"{relative_path}:{node.lineno}:{module}")
    return violations
