from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_EVAL_DATASET_PATH = Path("data/evals/agent_production_readiness_cases.json")
DEFAULT_EVAL_BACKLOG_PATH = Path("data/evals/agent_eval_backlog.json")
DEFAULT_EVAL_REVIEW_QUEUE_PATH = Path("data/evals/agent_eval_review_queue.json")

REQUIRED_EVAL_FIELDS = {
    "id",
    "title",
    "layer",
    "intent",
    "risk",
    "input",
    "expected_behavior",
    "prohibited_behavior",
    "tags",
    "owner",
    "severity",
    "synthetic",
    "review_cadence",
    "pass_gate",
}
REQUIRED_EVAL_TAGS = {
    "async",
    "evaluation",
    "fallback",
    "governance",
    "incident",
    "medication",
    "mcp",
    "nutrition",
    "observability",
    "patient_id",
    "phr",
    "privacy",
    "safety",
    "side_effect",
}
SENSITIVE_EVAL_MARKERS = ("Bearer ", "AWS_SECRET_ACCESS_KEY", "010-1234-5678", "user@example.com")
VALID_EVAL_LAYERS = {"deterministic", "semantic", "behavioral"}
VALID_EVAL_SEVERITIES = {"critical", "high", "medium", "low"}
BLOCKING_BACKLOG_SEVERITIES = {"critical", "high"}
VALID_EVAL_BACKLOG_LIFECYCLE_STATUSES = {"open", "reviewed", "cleared"}
DEFAULT_EVAL_BACKLOG_LIFECYCLE_STATUS = "open"
VALID_EVAL_REVIEW_STATUSES = {"open", "reviewed", "cleared"}


def load_eval_case_file(path: Path, *, missing_ok: bool = False) -> list[dict[str, Any]]:
    if missing_ok and not path.exists():
        return []
    try:
        raw_cases = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return []
        raise SystemExit(f"eval dataset not found: {path}") from None
    except ValueError as exc:
        raise SystemExit(f"eval dataset must be valid JSON: {path}: {exc}") from exc
    if not isinstance(raw_cases, list):
        raise SystemExit(f"eval dataset must be a list: {path}")
    return [case for case in raw_cases if isinstance(case, dict)]


def eval_case_schema_findings(case: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    missing_fields = sorted(REQUIRED_EVAL_FIELDS - set(case))
    if missing_fields:
        findings.append({"code": "missing_required_fields", "fields": missing_fields})
    if case.get("layer") not in VALID_EVAL_LAYERS:
        findings.append({"code": "invalid_layer", "actual": case.get("layer")})
    if case.get("severity") not in VALID_EVAL_SEVERITIES:
        findings.append({"code": "invalid_severity", "actual": case.get("severity")})
    if case.get("synthetic") is not True:
        findings.append({"code": "case_must_be_synthetic"})
    if not str(case.get("expected_behavior") or "").strip():
        findings.append({"code": "missing_expected_behavior"})
    if not str(case.get("prohibited_behavior") or "").strip():
        findings.append({"code": "missing_prohibited_behavior"})
    if not isinstance(case.get("tags"), list) or not case.get("tags"):
        findings.append({"code": "missing_tags"})
    if not isinstance(case.get("pass_gate"), dict) or not case.get("pass_gate"):
        findings.append({"code": "missing_pass_gate"})
    lifecycle = case.get("lifecycle")
    if isinstance(lifecycle, dict):
        status = str(lifecycle.get("status") or "").strip()
        if status and status not in VALID_EVAL_BACKLOG_LIFECYCLE_STATUSES:
            findings.append({"code": "invalid_lifecycle_status", "actual": status})
    return findings


def eval_case_id(case: dict[str, Any]) -> str:
    return str(case.get("id") or "").strip()


def eval_case_lifecycle_status(case: dict[str, Any]) -> str:
    lifecycle = case.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return DEFAULT_EVAL_BACKLOG_LIFECYCLE_STATUS
    status = str(lifecycle.get("status") or "").strip()
    if status in VALID_EVAL_BACKLOG_LIFECYCLE_STATUSES:
        return status
    return DEFAULT_EVAL_BACKLOG_LIFECYCLE_STATUS


def eval_case_is_cleared(case: dict[str, Any]) -> bool:
    return eval_case_lifecycle_status(case) == "cleared"


def is_blocking_backlog_case(case: dict[str, Any]) -> bool:
    return str(case.get("severity") or "") in BLOCKING_BACKLOG_SEVERITIES and not eval_case_is_cleared(case)
