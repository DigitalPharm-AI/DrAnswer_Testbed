from __future__ import annotations

import json

from shared.eval_cases import DEFAULT_EVAL_DATASET_PATH, load_eval_case_file
from tools.manage_eval_review_queue import main as review_main
from tools.run_agent_eval_suite import main


def test_agent_eval_runner_passes_deterministic_subset(tmp_path):
    output_path = tmp_path / "deterministic-report.json"

    exit_code = main(["--deterministic-only", "--output", str(output_path)])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["status"] == "ok"
    assert report["selected_count"] > 0
    assert report["summary"]["failed"] == 0
    assert report["summary"]["review_required"] == 0
    assert all(result["layer"] == "deterministic" for result in report["results"])
    assert all(result["status"] == "passed" for result in report["results"])


def test_agent_eval_runner_reports_review_for_full_suite(tmp_path):
    output_path = tmp_path / "full-report.json"

    exit_code = main(["--output", str(output_path)])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["status"] == "needs_review"
    assert report["summary"]["failed"] == 0
    assert report["summary"]["review_required"] > 0


def test_agent_eval_runner_accepts_human_review_cleared_semantic_cases(tmp_path):
    review_path = tmp_path / "review-state.json"
    output_path = tmp_path / "cleared-review-report.json"
    review_items = [
        {
            "case_id": case["id"],
            "status": "cleared",
            "reviewer": "qa-owner",
            "evidence": "Human review verified expected behavior and no prohibited behavior.",
        }
        for case in load_eval_case_file(DEFAULT_EVAL_DATASET_PATH)
        if case.get("layer") != "deterministic"
    ]
    review_path.write_text(json.dumps({"items": review_items}), encoding="utf-8")

    exit_code = main(["--no-backlog", "--review-state", str(review_path), "--output", str(output_path)])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["status"] == "ok"
    assert report["summary"]["review_required"] == 0
    assert report["review_state"]["cleared_count"] == len(review_items)
    assert any(result["findings"][0]["code"] == "human_review_cleared" for result in report["results"])


def test_eval_review_queue_export_and_mark_cleared(tmp_path):
    queue_path = tmp_path / "review-queue.json"

    export_exit = review_main(["export", "--no-backlog", "--output", str(queue_path)])
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    target_case_id = queue["items"][0]["case_id"]
    mark_exit = review_main(
        [
            "mark",
            "--queue",
            str(queue_path),
            "--case-id",
            target_case_id,
            "--status",
            "cleared",
            "--reviewer",
            "qa-owner",
            "--evidence",
            "Reviewed response transcript and attached regression run artifact.",
        ]
    )

    updated = json.loads(queue_path.read_text(encoding="utf-8"))
    target = next(item for item in updated["items"] if item["case_id"] == target_case_id)
    assert export_exit == 0
    assert mark_exit == 0
    assert target["status"] == "cleared"
    assert target["reviewer"] == "qa-owner"
    assert target["evidence"]
    assert target["history"][0]["previous_status"] == "open"


def test_agent_eval_runner_ingests_backlog_and_blocks_high_severity(tmp_path):
    backlog_path = tmp_path / "agent_eval_backlog.json"
    output_path = tmp_path / "backlog-report.json"
    backlog_path.write_text(json.dumps([_case("prod-trace-high-001", severity="high", layer="behavioral")]), encoding="utf-8")

    exit_code = main(["--backlog", str(backlog_path), "--output", str(output_path)])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    backlog_result = next(result for result in report["results"] if result["id"] == "prod-trace-high-001")
    assert exit_code == 1
    assert report["status"] == "failed"
    assert report["backlog"]["raw_count"] == 1
    assert report["backlog"]["included_count"] == 1
    assert report["backlog"]["blocking_count"] == 1
    assert backlog_result["source"] == "backlog"
    assert backlog_result["status"] == "failed"
    assert backlog_result["lifecycle_status"] == "open"
    assert backlog_result["findings"][0]["code"] == "high_severity_backlog_case_blocks_ci"


def test_agent_eval_runner_allows_cleared_high_severity_backlog_case(tmp_path):
    backlog_path = tmp_path / "agent_eval_backlog.json"
    output_path = tmp_path / "cleared-backlog-report.json"
    case = _case("prod-trace-cleared-001", severity="high", layer="deterministic")
    case["lifecycle"] = {"status": "cleared", "reason": "regression evidence attached"}
    backlog_path.write_text(json.dumps([case]), encoding="utf-8")

    exit_code = main(["--backlog", str(backlog_path), "--deterministic-only", "--output", str(output_path)])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    backlog_result = next(result for result in report["results"] if result["id"] == "prod-trace-cleared-001")
    assert exit_code == 0
    assert report["status"] == "ok"
    assert report["backlog"]["blocking_count"] == 0
    assert report["backlog"]["lifecycle_counts"]["cleared"] == 1
    assert backlog_result["status"] == "passed"
    assert backlog_result["lifecycle_status"] == "cleared"


def test_agent_eval_runner_validates_backlog_case_schema(tmp_path):
    backlog_path = tmp_path / "agent_eval_backlog.json"
    output_path = tmp_path / "invalid-backlog-report.json"
    backlog_path.write_text(json.dumps([{"id": "prod-trace-invalid"}]), encoding="utf-8")

    exit_code = main(["--backlog", str(backlog_path), "--output", str(output_path)])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    invalid_result = next(result for result in report["results"] if result["id"] == "prod-trace-invalid")
    assert exit_code == 1
    assert invalid_result["source"] == "backlog"
    assert invalid_result["status"] == "failed"
    assert invalid_result["findings"][0]["code"] == "missing_required_fields"


def test_agent_eval_runner_preserves_duplicate_stable_backlog_ids(tmp_path):
    backlog_path = tmp_path / "agent_eval_backlog.json"
    output_path = tmp_path / "duplicate-backlog-report.json"
    case = _case("prod-trace-duplicate-001", severity="low", layer="deterministic")
    backlog_path.write_text(json.dumps([case, case]), encoding="utf-8")

    exit_code = main(["--backlog", str(backlog_path), "--deterministic-only", "--output", str(output_path)])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    included = [result for result in report["results"] if result["id"] == "prod-trace-duplicate-001"]
    assert exit_code == 0
    assert len(included) == 1
    assert report["backlog"]["included_count"] == 1
    assert report["backlog"]["excluded_count"] == 1
    assert report["backlog"]["excluded"][0]["id"] == "prod-trace-duplicate-001"
    assert report["backlog"]["excluded"][0]["stable_id_preserved"] is True


def _case(case_id: str, *, severity: str, layer: str) -> dict:
    return {
        "id": case_id,
        "title": f"Backlog case {case_id}",
        "layer": layer,
        "intent": "incident_regression",
        "risk": "async_job_failure",
        "input": {"source_type": "trace", "trace_id": case_id, "summary": "synthetic incident"},
        "expected_behavior": "The agent keeps bounded retries and exposes trace evidence.",
        "prohibited_behavior": "The agent must not retry forever or hide the failed workflow.",
        "tags": ["evaluation", "observability", "incident", "async"],
        "owner": "eval-owner",
        "severity": severity,
        "synthetic": True,
        "review_cadence": "incident-review",
        "pass_gate": {"trace_replay_required": True},
    }
