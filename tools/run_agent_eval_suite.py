from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.eval_cases import (  # noqa: E402
    DEFAULT_EVAL_BACKLOG_PATH,
    DEFAULT_EVAL_DATASET_PATH,
    DEFAULT_EVAL_REVIEW_QUEUE_PATH,
    REQUIRED_EVAL_TAGS,
    SENSITIVE_EVAL_MARKERS,
    VALID_EVAL_REVIEW_STATUSES,
    eval_case_id,
    eval_case_lifecycle_status,
    eval_case_schema_findings,
    is_blocking_backlog_case,
    load_eval_case_file,
)

CASE_SOURCE_KEY = "_eval_source"
CANONICAL_SOURCE = "canonical"
BACKLOG_SOURCE = "backlog"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local AI agent production-readiness eval suite.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_EVAL_DATASET_PATH)
    parser.add_argument("--backlog", type=Path, default=DEFAULT_EVAL_BACKLOG_PATH)
    parser.add_argument("--no-backlog", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--deterministic-only", action="store_true")
    parser.add_argument("--fail-on-review-required", action="store_true")
    parser.add_argument("--review-state", type=Path, default=DEFAULT_EVAL_REVIEW_QUEUE_PATH)
    args = parser.parse_args(argv)

    canonical_cases = _annotate_cases(load_eval_case_file(args.dataset), CANONICAL_SOURCE)
    raw_backlog_cases = [] if args.no_backlog else load_eval_case_file(args.backlog, missing_ok=True)
    backlog_merge = _merge_backlog_cases(canonical_cases, _annotate_cases(raw_backlog_cases, BACKLOG_SOURCE))
    cases = backlog_merge["cases"]
    selected_cases = [case for case in cases if case.get("layer") == "deterministic"] if args.deterministic_only else cases
    review_state = _load_review_state(args.review_state)
    results = [_evaluate_case(case, review_state["items_by_case_id"]) for case in selected_cases]
    dataset_findings = _dataset_findings(args.dataset, cases)

    failed_count = sum(1 for result in results if result["status"] == "failed") + len(dataset_findings)
    review_count = sum(1 for result in results if result["status"] == "review_required")
    passed_count = sum(1 for result in results if result["status"] == "passed")
    status = "failed" if failed_count else "needs_review" if review_count else "ok"
    report = {
        "status": status,
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset_path": str(args.dataset),
        "backlog_path": str(args.backlog),
        "case_count": len(cases),
        "selected_count": len(selected_cases),
        "deterministic_only": args.deterministic_only,
        "summary": {
            "passed": passed_count,
            "failed": failed_count,
            "review_required": review_count,
        },
        "coverage": {
            "critical_count": sum(1 for case in cases if case.get("severity") == "critical"),
            "tags": sorted({str(tag) for case in cases for tag in case.get("tags", [])}),
        },
        "backlog": {
            "enabled": not args.no_backlog,
            "raw_count": len(raw_backlog_cases),
            "included_count": backlog_merge["included_count"],
            "excluded_count": len(backlog_merge["excluded"]),
            "excluded": backlog_merge["excluded"],
            "blocking_count": sum(1 for result in results if result["status"] == "failed" and result.get("source") == BACKLOG_SOURCE),
            "lifecycle_counts": {
                status: sum(1 for case in raw_backlog_cases if eval_case_lifecycle_status(case) == status)
                for status in ("open", "reviewed", "cleared")
            },
        },
        "review_state": {
            "path": str(args.review_state),
            "exists": review_state["exists"],
            "raw_count": review_state["raw_count"],
            "case_count": len(review_state["items_by_case_id"]),
            "status_counts": review_state["status_counts"],
            "cleared_count": review_state["status_counts"].get("cleared", 0),
            "selected_review_required_count": review_count,
        },
        "dataset_findings": dataset_findings,
        "results": results,
    }

    output_path = args.output or _default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": status, "output": str(output_path), "summary": report["summary"]}, ensure_ascii=False))

    if failed_count:
        return 1
    if review_count and args.fail_on_review_required:
        return 2
    return 0


def _annotate_cases(cases: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [{**case, CASE_SOURCE_KEY: source} for case in cases]


def _merge_backlog_cases(canonical_cases: list[dict[str, Any]], backlog_cases: list[dict[str, Any]]) -> dict[str, Any]:
    merged = list(canonical_cases)
    seen_ids = {eval_case_id(case) for case in canonical_cases if eval_case_id(case)}
    excluded: list[dict[str, Any]] = []
    included_count = 0
    for case in backlog_cases:
        case_id = eval_case_id(case)
        if not case_id:
            merged.append(case)
            included_count += 1
            continue
        if case_id in seen_ids:
            excluded.append(
                {
                    "id": case_id,
                    "reason": "duplicate_case_id",
                    "source": BACKLOG_SOURCE,
                    "stable_id_preserved": True,
                }
            )
            continue
        seen_ids.add(case_id)
        merged.append(case)
        included_count += 1
    return {"cases": merged, "included_count": included_count, "excluded": excluded}


def _load_review_state(path: Path) -> dict[str, Any]:
    empty = {
        "exists": False,
        "raw_count": 0,
        "items_by_case_id": {},
        "status_counts": {status: 0 for status in sorted(VALID_EVAL_REVIEW_STATUSES)},
    }
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"eval review state must be valid JSON: {path}: {exc}") from exc
    raw_items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(raw_items, list):
        raise SystemExit(f"eval review state must contain an items list: {path}")

    items_by_case_id: dict[str, dict[str, Any]] = {}
    status_counts = {status: 0 for status in sorted(VALID_EVAL_REVIEW_STATUSES)}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or item.get("id") or "").strip()
        if not case_id:
            continue
        status = str(item.get("status") or "open").strip()
        if status not in VALID_EVAL_REVIEW_STATUSES:
            status = "open"
        normalized = {**item, "case_id": case_id, "status": status}
        items_by_case_id[case_id] = normalized
        status_counts[status] += 1
    return {
        "exists": True,
        "raw_count": len(raw_items),
        "items_by_case_id": items_by_case_id,
        "status_counts": status_counts,
    }


def _dataset_findings(path: Path, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    ids = [str(case.get("id") or "") for case in cases]
    duplicate_ids = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicate_ids:
        findings.append({"code": "duplicate_case_ids", "severity": "critical", "ids": duplicate_ids})
    observed_tags = {str(tag) for case in cases for tag in case.get("tags", [])}
    missing_tags = sorted(REQUIRED_EVAL_TAGS - observed_tags)
    if missing_tags:
        findings.append({"code": "missing_required_coverage_tags", "severity": "critical", "tags": missing_tags})
    critical_count = sum(1 for case in cases if case.get("severity") == "critical")
    if len(cases) < 30:
        findings.append({"code": "minimum_case_count_not_met", "severity": "critical", "actual": len(cases), "threshold": 30})
    if critical_count < 15:
        findings.append({"code": "minimum_critical_case_count_not_met", "severity": "critical", "actual": critical_count, "threshold": 15})
    raw = path.read_text(encoding="utf-8")
    leaked_markers = [marker for marker in SENSITIVE_EVAL_MARKERS if marker in raw]
    if leaked_markers:
        findings.append({"code": "sensitive_marker_present", "severity": "critical", "markers": leaked_markers})
    return findings


def _evaluate_case(case: dict[str, Any], review_items: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    findings = _case_schema_findings(case)
    if findings:
        return _result(case, "failed", findings)
    if case.get(CASE_SOURCE_KEY) == BACKLOG_SOURCE and is_blocking_backlog_case(case):
        return _result(
            case,
            "failed",
            [
                {
                    "code": "high_severity_backlog_case_blocks_ci",
                    "severity": case.get("severity"),
                    "lifecycle_status": eval_case_lifecycle_status(case),
                    "expected_action": "owner review plus regression evidence before clearing the gate",
                }
            ],
        )
    if case.get("layer") != "deterministic":
        review = (review_items or {}).get(eval_case_id(case))
        if review:
            review_status = str(review.get("status") or "open")
            evidence = str(review.get("evidence") or "").strip()
            if review_status == "cleared" and evidence:
                return _result(
                    case,
                    "passed",
                    [
                        {
                            "code": "human_review_cleared",
                            "reviewer": review.get("reviewer") or "",
                            "evidence": evidence[:240],
                        }
                    ],
                )
            if review_status == "cleared":
                return _result(case, "review_required", [{"code": "human_review_evidence_missing"}])
            if review_status == "reviewed":
                return _result(
                    case,
                    "review_required",
                    [
                        {
                            "code": "human_review_not_cleared",
                            "reviewer": review.get("reviewer") or "",
                        }
                    ],
                )
        return _result(case, "review_required", [{"code": "semantic_or_behavioral_review_required"}])
    pass_gate = case.get("pass_gate") if isinstance(case.get("pass_gate"), dict) else {}
    if not pass_gate:
        return _result(case, "failed", [{"code": "missing_pass_gate"}])
    return _result(case, "passed", [{"code": "deterministic_gate_defined", "gate_keys": sorted(pass_gate.keys())}])


def _case_schema_findings(case: dict[str, Any]) -> list[dict[str, Any]]:
    return eval_case_schema_findings(case)


def _result(case: dict[str, Any], status: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": case.get("id", ""),
        "title": case.get("title", ""),
        "layer": case.get("layer", ""),
        "severity": case.get("severity", ""),
        "intent": case.get("intent", ""),
        "source": case.get(CASE_SOURCE_KEY, CANONICAL_SOURCE),
        "lifecycle_status": eval_case_lifecycle_status(case),
        "status": status,
        "findings": findings,
    }


def _default_output_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("outputs/evals") / f"agent-eval-{stamp}.json"


if __name__ == "__main__":
    sys.exit(main())
