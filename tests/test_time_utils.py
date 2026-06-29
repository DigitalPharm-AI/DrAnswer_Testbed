from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from shared.time_utils import utc_now

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IGNORED_SCAN_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "outputs"}


def test_utc_now_returns_naive_utc_for_existing_datetime_columns():
    before = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    value = utc_now()
    after = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)

    assert value.tzinfo is None
    assert before <= value <= after


def test_project_does_not_call_deprecated_datetime_utcnow_directly():
    needle = "datetime." + "utcnow"
    offenders = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in IGNORED_SCAN_DIRS for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if needle in text:
            offenders.append(str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"))

    assert offenders == []
