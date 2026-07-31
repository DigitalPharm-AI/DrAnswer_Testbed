from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REMOVED_COMPATIBILITY_MODULES = {
    "agent_app.integration.chat_contracts",
    "agent_app.integration.contracts",
    "agent_app.orchestration.confirmations",
    "agent_app.tools.catalog",
    "agent_app.tools.names",
    "agent_app.tools.permissions",
}


def test_system_app_has_no_static_agent_app_imports() -> None:
    violations = _imports_from(REPO_ROOT / "system_app", forbidden_roots={"agent_app"})

    assert violations == []


def test_shared_contracts_do_not_depend_on_application_packages() -> None:
    violations = _imports_from(
        REPO_ROOT / "shared",
        forbidden_roots={"agent_app", "system_app"},
    )

    assert violations == []


def test_application_code_imports_shared_contracts_directly() -> None:
    violations = [
        *_imports_exact(REPO_ROOT / "agent_app", forbidden_modules=REMOVED_COMPATIBILITY_MODULES),
        *_imports_exact(REPO_ROOT / "tests", forbidden_modules=REMOVED_COMPATIBILITY_MODULES),
    ]

    assert violations == []


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


def _imports_exact(root: Path, *, forbidden_modules: set[str]) -> list[str]:
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
                if module in forbidden_modules:
                    relative_path = path.relative_to(REPO_ROOT).as_posix()
                    violations.append(f"{relative_path}:{node.lineno}:{module}")
    return violations
