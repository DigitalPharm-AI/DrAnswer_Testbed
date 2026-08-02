from __future__ import annotations

import json
from pathlib import Path

DATASET_PATH = Path("data/evals/agent_production_readiness_cases.json")
REQUIRED_FIELDS = {
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
REQUIRED_TAGS = {
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
    "patient_snapshot",
    "privacy",
    "safety",
    "side_effect",
}


def test_production_readiness_eval_dataset_has_schema_and_required_coverage():
    cases = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    assert len(cases) >= 30
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))

    observed_tags: set[str] = set()
    critical_count = 0
    for case in cases:
        assert REQUIRED_FIELDS <= set(case)
        assert case["layer"] in {"deterministic", "semantic", "behavioral"}
        assert case["severity"] in {"critical", "high", "medium", "low"}
        assert case["synthetic"] is True
        assert case["expected_behavior"].strip()
        assert case["prohibited_behavior"].strip()
        assert isinstance(case["tags"], list) and case["tags"]
        assert isinstance(case["pass_gate"], dict) and case["pass_gate"]
        observed_tags.update(str(tag) for tag in case["tags"])
        critical_count += 1 if case["severity"] == "critical" else 0

    assert REQUIRED_TAGS <= observed_tags
    assert critical_count >= 15


def test_production_readiness_eval_dataset_has_no_separate_phr_registration_flow():
    cases = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(cases, ensure_ascii=False).lower()

    assert "phr registration" not in serialized
    assert "phr 등록" not in serialized
    assert "phr not synced" not in serialized
    assert "phr_read_only" not in serialized


def test_production_readiness_eval_dataset_does_not_embed_real_sensitive_markers():
    raw = DATASET_PATH.read_text(encoding="utf-8")

    assert "Bearer " not in raw
    assert "AWS_SECRET_ACCESS_KEY" not in raw
    assert "010-1234-5678" not in raw
    assert "user@example.com" not in raw
