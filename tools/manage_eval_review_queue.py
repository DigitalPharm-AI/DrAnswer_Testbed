from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.eval_cases import (
    DEFAULT_EVAL_BACKLOG_PATH,
    DEFAULT_EVAL_DATASET_PATH,
    DEFAULT_EVAL_REVIEW_QUEUE_PATH,
    VALID_EVAL_REVIEW_STATUSES,
    eval_case_id,
    load_eval_case_file,
)

REVIEWABLE_LAYERS = {"semantic", "behavioral"}
SCHEMA_VERSION = "agent_eval_review_queue_v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage human review state for semantic/behavioral agent evals.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export or refresh the review_required queue.")
    export_parser.add_argument("--dataset", type=Path, default=DEFAULT_EVAL_DATASET_PATH)
    export_parser.add_argument("--backlog", type=Path, default=DEFAULT_EVAL_BACKLOG_PATH)
    export_parser.add_argument("--no-backlog", action="store_true")
    export_parser.add_argument("--output", type=Path, default=DEFAULT_EVAL_REVIEW_QUEUE_PATH)

    list_parser = subparsers.add_parser("list", help="Print review queue summary.")
    list_parser.add_argument("--queue", type=Path, default=DEFAULT_EVAL_REVIEW_QUEUE_PATH)

    mark_parser = subparsers.add_parser("mark", help="Mark one review item as open, reviewed, or cleared.")
    mark_parser.add_argument("--queue", type=Path, default=DEFAULT_EVAL_REVIEW_QUEUE_PATH)
    mark_parser.add_argument("--case-id", required=True)
    mark_parser.add_argument("--status", required=True, choices=sorted(VALID_EVAL_REVIEW_STATUSES))
    mark_parser.add_argument("--reviewer", default="")
    mark_parser.add_argument("--evidence", default="")
    mark_parser.add_argument("--notes", default="")

    args = parser.parse_args(argv)
    if args.command == "export":
        return _export_queue(args.dataset, args.backlog, args.output, include_backlog=not args.no_backlog)
    if args.command == "list":
        return _list_queue(args.queue)
    if args.command == "mark":
        return _mark_item(args.queue, args.case_id, args.status, args.reviewer, args.evidence, args.notes)
    raise SystemExit(f"unsupported command: {args.command}")


def _export_queue(dataset_path: Path, backlog_path: Path, output_path: Path, *, include_backlog: bool) -> int:
    canonical_cases = _annotate_source(load_eval_case_file(dataset_path), "canonical")
    backlog_cases = [] if not include_backlog else _annotate_source(load_eval_case_file(backlog_path, missing_ok=True), "backlog")
    cases = _dedupe_cases(canonical_cases + backlog_cases)
    reviewable_cases = [case for case in cases if case.get("layer") in REVIEWABLE_LAYERS]

    existing = _load_queue(output_path)
    existing_by_id = _items_by_case_id(existing.get("items", []))
    active_ids = {eval_case_id(case) for case in reviewable_cases if eval_case_id(case)}
    now = _utc_now()
    refreshed_items: list[dict[str, Any]] = []

    for case in reviewable_cases:
        case_id = eval_case_id(case)
        if not case_id:
            continue
        current = existing_by_id.get(case_id, {})
        refreshed_items.append(
            {
                **_case_review_item(case, now),
                "status": _valid_status(current.get("status")),
                "reviewer": str(current.get("reviewer") or ""),
                "evidence": str(current.get("evidence") or ""),
                "notes": str(current.get("notes") or ""),
                "created_at": str(current.get("created_at") or now),
                "updated_at": str(current.get("updated_at") or now),
                "history": current.get("history") if isinstance(current.get("history"), list) else [],
                "active": True,
            }
        )

    for case_id, item in existing_by_id.items():
        if case_id not in active_ids:
            refreshed_items.append({**item, "active": False, "updated_at": now})

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "dataset_path": str(dataset_path),
        "backlog_path": str(backlog_path) if include_backlog else "",
        "items": sorted(refreshed_items, key=lambda item: (str(item.get("status")), str(item.get("case_id")))),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "output": str(output_path), "summary": _queue_summary(payload)}, ensure_ascii=False))
    return 0


def _list_queue(queue_path: Path) -> int:
    payload = _load_queue(queue_path)
    print(json.dumps({"status": "ok", "queue": str(queue_path), "summary": _queue_summary(payload)}, ensure_ascii=False))
    return 0


def _mark_item(queue_path: Path, case_id: str, status: str, reviewer: str, evidence: str, notes: str) -> int:
    payload = _load_queue(queue_path)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    target = next((item for item in items if isinstance(item, dict) and str(item.get("case_id") or "") == case_id), None)
    if target is None:
        raise SystemExit(f"review item not found: {case_id}; run export first")
    if status == "cleared" and not evidence.strip():
        raise SystemExit("cleared review items require --evidence")

    now = _utc_now()
    history = target.get("history") if isinstance(target.get("history"), list) else []
    history.append(
        {
            "at": now,
            "previous_status": _valid_status(target.get("status")),
            "new_status": status,
            "reviewer": reviewer,
            "evidence": evidence[:240],
            "notes": notes[:240],
        }
    )
    target.update(
        {
            "status": status,
            "reviewer": reviewer,
            "evidence": evidence,
            "notes": notes,
            "updated_at": now,
            "history": history,
        }
    )
    payload["generated_at"] = now
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "queue": str(queue_path), "case_id": case_id, "new_status": status}, ensure_ascii=False))
    return 0


def _annotate_source(cases: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [{**case, "source": source} for case in cases]


def _dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for case in cases:
        case_id = eval_case_id(case)
        if case_id and case_id in seen:
            continue
        if case_id:
            seen.add(case_id)
        deduped.append(case)
    return deduped


def _case_review_item(case: dict[str, Any], now: str) -> dict[str, Any]:
    return {
        "case_id": eval_case_id(case),
        "title": str(case.get("title") or ""),
        "layer": str(case.get("layer") or ""),
        "severity": str(case.get("severity") or ""),
        "owner": str(case.get("owner") or ""),
        "intent": str(case.get("intent") or ""),
        "risk": str(case.get("risk") or ""),
        "source": str(case.get("source") or ""),
        "tags": case.get("tags") if isinstance(case.get("tags"), list) else [],
        "status": "open",
        "reviewer": "",
        "evidence": "",
        "notes": "",
        "created_at": now,
        "updated_at": now,
        "history": [],
        "active": True,
    }


def _load_queue(queue_path: Path) -> dict[str, Any]:
    if not queue_path.exists():
        return {"schema_version": SCHEMA_VERSION, "generated_at": "", "items": []}
    try:
        payload = json.loads(queue_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"review queue must be valid JSON: {queue_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"review queue must be a JSON object: {queue_path}")
    if not isinstance(payload.get("items"), list):
        payload["items"] = []
    return payload


def _items_by_case_id(items: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or item.get("id") or "").strip()
        if case_id:
            result[case_id] = {**item, "case_id": case_id}
    return result


def _queue_summary(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    status_counts = {status: 0 for status in sorted(VALID_EVAL_REVIEW_STATUSES)}
    active_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        status_counts[_valid_status(item.get("status"))] += 1
        if item.get("active", True):
            active_count += 1
    return {"total": len(items), "active": active_count, "status_counts": status_counts}


def _valid_status(value: object) -> str:
    status = str(value or "open").strip()
    return status if status in VALID_EVAL_REVIEW_STATUSES else "open"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    sys.exit(main())
