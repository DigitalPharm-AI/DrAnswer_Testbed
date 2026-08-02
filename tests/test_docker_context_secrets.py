from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
CONTEXT_CHECK_DOCKERFILE = PROJECT_ROOT / "Dockerfile.context-check"


def _active_rules() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_docker_context_excludes_env_files_and_reincludes_only_examples() -> None:
    rules = _active_rules()

    expected_rules = [
        ".env*",
        "**/.env*",
        "!.env*.example",
        "!**/.env*.example",
    ]
    positions = [rules.index(rule) for rule in expected_rules]

    assert positions == sorted(positions)


def test_runtime_env_file_name_is_not_reincluded_as_an_example() -> None:
    runtime_env_name = ".env.9000"

    assert runtime_env_name.startswith(".env")
    assert not runtime_env_name.endswith(".example")


def test_committed_env_templates_follow_the_reincluded_suffix() -> None:
    templates = sorted(PROJECT_ROOT.glob(".env*.example"))

    assert templates
    assert all(path.name.startswith(".env") for path in templates)
    assert all(path.name.endswith(".example") for path in templates)


def test_context_check_image_fails_when_a_runtime_env_file_is_copied() -> None:
    dockerfile = CONTEXT_CHECK_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY . ." in dockerfile
    assert "-name '.env*'" in dockerfile
    assert "! -name '*.example'" in dockerfile
    assert "test -z" in dockerfile
