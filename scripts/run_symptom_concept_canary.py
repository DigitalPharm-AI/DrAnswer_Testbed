from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from agent_app.ae_pro_ctcae import (
    pro_ctcae_result_for_concept,
    pro_ctcae_source_version,
)
from agent_app.embeddings.base import EmbeddingProvider
from agent_app.embeddings.factory import get_embedding_provider
from agent_app.embeddings.semantic_verifier import (
    LlmSemanticMatchVerifier,
    SemanticCandidate,
    SemanticMatchVerifier,
)
from agent_app.persistence.db import engine
from agent_app.persistence.schema import verify_agent_schema_current
from agent_app.persistence.symptom_concept_repository import (
    SymptomConceptRepository,
)
from agent_app.providers.factory import create_llm_provider
from shared.settings import get_settings

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "evals"
    / "symptom_concept_canary_cases.json"
)


@dataclass
class CountingEmbeddingProvider:
    delegate: EmbeddingProvider
    query_calls: int = 0
    document_calls: int = 0

    @property
    def identity(self):
        return self.delegate.identity

    async def embed_query(self, text_value: str) -> list[float]:
        self.query_calls += 1
        return await self.delegate.embed_query(text_value)

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self.document_calls += 1
        return await self.delegate.embed_documents(texts)


@dataclass
class CountingSemanticVerifier:
    delegate: SemanticMatchVerifier
    calls: int = 0

    async def matching_candidate_ids(
        self,
        query: str,
        candidates: list[SemanticCandidate],
        *,
        domain: str,
    ) -> set[str]:
        self.calls += 1
        return await self.delegate.matching_candidate_ids(
            query,
            candidates,
            domain=domain,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the versioned input-to-concept-to-reference canary suite."
        )
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Run only the selected case ID. May be supplied repeatedly.",
    )
    return parser.parse_args()


def _load_cases(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("symptom_concept_canary_cases_required")
    case_ids = [str(item.get("case_id") or "") for item in cases]
    if any(not value for value in case_ids):
        raise ValueError("symptom_concept_canary_case_id_required")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("symptom_concept_canary_case_id_duplicate")
    return payload


async def _run_case(
    case: dict[str, Any],
    *,
    repository: SymptomConceptRepository,
    pro_source: str,
    mfds_source: str,
    workbook_path: Path,
    run_id: str,
) -> dict[str, Any]:
    settings = get_settings()
    embedding = CountingEmbeddingProvider(get_embedding_provider())
    verifier = CountingSemanticVerifier(
        LlmSemanticMatchVerifier(create_llm_provider())
    )
    symptom_text = str(case["input"])
    trace_id = f"symptom-canary:{run_id}:{case['case_id']}"
    started = perf_counter()
    concept = await repository.resolve(
        symptom_text=symptom_text,
        pro_ctcae_source_version=pro_source,
        embedding_provider=embedding,
        semantic_verifier=verifier,
        top_k=settings.pro_ctcae_vector_top_k,
        min_similarity=settings.reference_vector_min_similarity,
    )

    linked_terms: list[str] = []
    question_count = 0
    recall_matches = False
    if concept is not None:
        linked_terms = [
            candidate.normalized_term
            for candidate in repository.linked_reaction_candidates(
                concept=concept,
                mfds_source_version=mfds_source,
            )
        ]
        repository.remember(
            trace_id=trace_id,
            symptom_text=symptom_text,
            concept=concept,
            ttl_seconds=settings.agent_symptom_resolution_ttl_seconds,
        )
        recalled = repository.recall(
            trace_id=trace_id,
            symptom_text=symptom_text,
            pro_ctcae_source_version=pro_source,
        )
        recall_matches = bool(
            recalled is not None
            and recalled.concept_id == concept.concept_id
            and recalled.match_type == concept.match_type
        )
        if recalled is not None:
            questionnaire = pro_ctcae_result_for_concept(
                symptom_text,
                recalled,
                workbook_path=workbook_path,
            )
            question_count = len(questionnaire.questions)

    expected_concept = case.get("expected_concept")
    expected_match_type = str(case.get("expected_match_type") or "")
    actual_concept = concept.symptom_term if concept is not None else None
    actual_match_type = concept.match_type if concept is not None else "NONE"
    failures: list[str] = []
    if actual_concept != expected_concept:
        failures.append(
            f"concept:{actual_concept!r}!={expected_concept!r}"
        )
    if actual_match_type != expected_match_type:
        failures.append(
            f"match_type:{actual_match_type}!={expected_match_type}"
        )
    if expected_match_type == "EXACT":
        if embedding.query_calls != 0:
            failures.append("exact_match_called_embedding")
        if verifier.calls != 0:
            failures.append("exact_match_called_llm_verifier")
    elif expected_match_type == "VECTOR_LLM_VERIFIED":
        if embedding.query_calls != 1:
            failures.append(
                f"semantic_embedding_calls:{embedding.query_calls}!=1"
            )
        if verifier.calls != 1:
            failures.append(
                f"semantic_verifier_calls:{verifier.calls}!=1"
            )
    if concept is not None and not recall_matches:
        failures.append("concept_recall_mismatch")
    if bool(case.get("require_mfds_link")) and not linked_terms:
        failures.append("mfds_prelinked_reaction_missing")
    if bool(case.get("require_pro_ctcae")) and question_count < 1:
        failures.append("pro_ctcae_question_missing")
    if expected_concept is None and (
        linked_terms or question_count or recall_matches
    ):
        failures.append("negative_case_created_downstream_state")

    return {
        "case_id": case["case_id"],
        "input": symptom_text,
        "tags": case.get("tags", []),
        "expected_concept": expected_concept,
        "actual_concept": actual_concept,
        "match_type": actual_match_type,
        "similarity": (
            round(float(concept.similarity), 6)
            if concept is not None
            else None
        ),
        "embedding_query_calls": embedding.query_calls,
        "semantic_verifier_calls": verifier.calls,
        "mfds_linked_reaction_count": len(linked_terms),
        "mfds_linked_reactions": linked_terms,
        "pro_ctcae_question_count": question_count,
        "concept_recall_matches": recall_matches,
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        "passed": not failures,
        "failures": failures,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    settings.require_agent_postgresql()
    verify_agent_schema_current(engine)
    dataset = _load_cases(args.dataset.resolve())
    selected_ids = set(args.case_ids or [])
    selected_cases = [
        case
        for case in dataset["cases"]
        if not selected_ids or str(case["case_id"]) in selected_ids
    ]
    found_ids = {str(case["case_id"]) for case in selected_cases}
    missing_ids = selected_ids - found_ids
    if missing_ids:
        raise ValueError(
            "symptom_concept_canary_case_unknown:"
            + ",".join(sorted(missing_ids))
        )
    if not selected_cases:
        raise ValueError("symptom_concept_canary_selection_empty")
    workbook_path = settings.pro_ctcae_workbook_path.resolve()
    pro_source = pro_ctcae_source_version(workbook_path)
    repository = SymptomConceptRepository(engine)
    mfds_source = repository.mfds_source_version()
    run_id = uuid4().hex
    cases: list[dict[str, Any]] = []
    try:
        for case in selected_cases:
            cases.append(
                await _run_case(
                    case,
                    repository=repository,
                    pro_source=pro_source,
                    mfds_source=mfds_source,
                    workbook_path=workbook_path,
                    run_id=run_id,
                )
            )
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    DELETE FROM agent_symptom_resolution_states
                    WHERE trace_id LIKE :trace_prefix
                    """
                ),
                {"trace_prefix": f"symptom-canary:{run_id}:%"},
            )

    passed = sum(1 for case in cases if case["passed"])
    result = {
        "dataset_id": dataset["dataset_id"],
        "selected_case_ids": [
            str(case["case_id"]) for case in selected_cases
        ],
        "run_id": run_id,
        "executed_at": datetime.now(UTC).isoformat(),
        "embedding_model": settings.embedding_model_id,
        "embedding_region": settings.embedding_aws_region,
        "semantic_verifier_model": settings.model_id_for_tier("fast"),
        "pro_ctcae_source_version": pro_source,
        "mfds_source_version": mfds_source,
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "pass_rate": round(passed / len(cases), 4),
        "cases": cases,
    }
    if args.output is not None:
        output_path = args.output.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
