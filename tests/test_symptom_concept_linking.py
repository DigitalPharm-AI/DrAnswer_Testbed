from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from agent_app.ae_pro_ctcae import (
    load_workbook,
    normalize_pro_ctcae_lookup_key,
    pro_ctcae_result_for_concept,
    pro_ctcae_source_version,
)
from agent_app.embeddings.base import EmbeddingIdentity
from agent_app.embeddings.concept_backfill import (
    AgentSymptomConceptBackfill,
)
from agent_app.embeddings.reference_text import (
    adverse_reaction_source_version,
)
from agent_app.embeddings.semantic_verifier import SemanticCandidate
from agent_app.persistence.schema import migrate_agent_schema
from agent_app.persistence.symptom_concept_repository import (
    ClinicalSymptomConceptMatch,
    SymptomConceptRepository,
)
from shared.settings import Settings
from tests.helpers import build_agent_engine

POSTGRES_CONFIGURED = bool(
    os.getenv("AGENT_POSTGRES_TEST_DATABASE_URL", "").strip()
)
VECTOR = "[1," + ",".join(["0"] * 1023) + "]"


class FakeEmbeddingProvider:
    identity = EmbeddingIdentity(
        provider="fake_cohere",
        model_id="fake-cohere-multilingual-v3",
        region="ap-northeast-1",
        dimensions=1024,
    )

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, text_value: str) -> list[float]:
        self.queries.append(text_value)
        return [1.0] + [0.0] * 1023

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        return [[1.0] + [0.0] * 1023 for _value in texts]


class SelectNauseaVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[SemanticCandidate], str]] = []

    async def matching_candidate_ids(
        self,
        query: str,
        candidates: list[SemanticCandidate],
        *,
        domain: str,
    ) -> set[str]:
        self.calls.append((query, candidates, domain))
        return {
            candidate.candidate_id
            for candidate in candidates
            if "Nausea" in candidate.label
        }


class SelectMfdsTermVerifier:
    async def matching_candidate_ids(
        self,
        query: str,
        candidates: list[SemanticCandidate],
        *,
        domain: str,
    ) -> set[str]:
        assert "Nausea" in query
        assert domain == "pro_ctcae_concept_to_mfds_reference_link"
        return {
            candidate.candidate_id
            for candidate in candidates
            if candidate.label == "속 부글거림"
        }


class SelectAllVerifier:
    async def matching_candidate_ids(
        self,
        query: str,
        candidates: list[SemanticCandidate],
        *,
        domain: str,
    ) -> set[str]:
        assert query == "붉은 반점"
        assert domain == "clinical_symptom_concept"
        return {candidate.candidate_id for candidate in candidates}


@pytest.mark.asyncio
async def test_runtime_concept_resolution_does_not_choose_first_when_ambiguous(
    monkeypatch,
):
    repository = SymptomConceptRepository(object())  # type: ignore[arg-type]
    candidates = [
        ClinicalSymptomConceptMatch(
            concept_id=1,
            symptom_term="Rash",
            korean_symptom_name="발진",
            sheet_name="Rash",
            aliases=("발진",),
            match_type="VECTOR_LLM_VERIFIED",
            similarity=0.91,
            pro_ctcae_source_version="pro-v1",
        ),
        ClinicalSymptomConceptMatch(
            concept_id=2,
            symptom_term="Hives",
            korean_symptom_name="두드러기",
            sheet_name="Hives",
            aliases=("두드러기",),
            match_type="VECTOR_LLM_VERIFIED",
            similarity=0.89,
            pro_ctcae_source_version="pro-v1",
        ),
    ]
    monkeypatch.setattr(repository, "_exact_match", lambda **_kwargs: None)
    monkeypatch.setattr(
        repository,
        "_vector_candidates",
        lambda **_kwargs: candidates,
    )

    resolution = await repository.resolve_with_status(
        symptom_text="붉은 반점",
        pro_ctcae_source_version="pro-v1",
        embedding_provider=FakeEmbeddingProvider(),
        semantic_verifier=SelectAllVerifier(),
        top_k=5,
        min_similarity=0.0,
    )

    assert resolution.status == "AMBIGUOUS"
    assert resolution.match is None
    assert [candidate.concept_id for candidate in resolution.candidates] == [
        1,
        2,
    ]


@pytest.mark.skipif(
    not POSTGRES_CONFIGURED,
    reason="AGENT_POSTGRES_TEST_DATABASE_URL is required",
)
@pytest.mark.asyncio
async def test_prelinked_concept_reuses_one_resolution_for_mfds_and_pro_ctcae():
    engine, cleanup = build_agent_engine("symptom_concept_link")
    try:
        migrate_agent_schema(engine)
        workbook_path = Path(Settings().pro_ctcae_workbook_path).resolve()
        workbook = load_workbook(workbook_path)
        nausea = next(
            entry
            for entry in workbook.parsed_entries
            if entry.symptom_term == "Nausea"
        )
        pro_source = pro_ctcae_source_version(workbook_path)
        mfds_sha = "a" * 64
        mfds_source = adverse_reaction_source_version(
            mfds_sha,
            "fixture-v1",
        )
        aliases = ["메스꺼움", "구역", "오심"]
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_import_runs (
                        run_id, source_file, source_sha256,
                        extractor_version, started_at, completed_at,
                        status, stats_json
                    ) VALUES (
                        1, 'fixture.sqlite', :source_sha,
                        'fixture-v1', CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, 'COMPLETED', '{}'
                    )
                    """
                ),
                {"source_sha": mfds_sha},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_adverse_reactions (
                        reaction_id, normalized_term
                    ) VALUES
                        (1, '메스꺼움'),
                        (2, '구역'),
                        (3, '속 부글거림')
                    """
                )
            )
            for reaction_id, source_text in enumerate(
                ("메스꺼움", "구역", "속 부글거림"),
                start=1,
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO mfds_adverse_reaction_embeddings (
                            reaction_id, source_text, normalized_text,
                            embedding, provider, model_id, model_region,
                            dimensions, source_version, source_sha256
                        ) VALUES (
                            :reaction_id, :source_text, :normalized_text,
                            CAST(:embedding AS public.vector),
                            'fake_cohere', :model_id, 'ap-northeast-1',
                            1024, :source_version, :source_sha
                        )
                        """
                    ),
                    {
                        "reaction_id": reaction_id,
                        "source_text": source_text,
                        "normalized_text": (
                            normalize_pro_ctcae_lookup_key(source_text)
                        ),
                        "embedding": VECTOR,
                        "model_id": (
                            FakeEmbeddingProvider.identity.model_id
                        ),
                        "source_version": mfds_source,
                        "source_sha": mfds_sha,
                    },
                )
            for alias in aliases:
                connection.execute(
                    text(
                        """
                        INSERT INTO agent_pro_ctcae_alias_embeddings (
                            symptom_term, korean_symptom_name,
                            sheet_name, alias_text, normalized_alias,
                            embedding, provider, model_id, model_region,
                            dimensions, source_version, source_sha256
                        ) VALUES (
                            :symptom_term, :korean_name,
                            :sheet_name, :alias_text, :normalized_alias,
                            CAST(:embedding AS public.vector),
                            'fake_cohere', :model_id, 'ap-northeast-1',
                            1024, :source_version, :source_sha
                        )
                        """
                    ),
                    {
                        "symptom_term": nausea.symptom_term,
                        "korean_name": nausea.korean_symptom_name,
                        "sheet_name": nausea.sheet_name,
                        "alias_text": alias,
                        "normalized_alias": (
                            normalize_pro_ctcae_lookup_key(alias)
                        ),
                        "embedding": VECTOR,
                        "model_id": (
                            FakeEmbeddingProvider.identity.model_id
                        ),
                        "source_version": pro_source,
                        "source_sha": "b" * 64,
                    },
                )

        provider = FakeEmbeddingProvider()
        build = AgentSymptomConceptBackfill(engine, provider).exact(
            workbook_path
        )
        assert build.concept_count == 1
        assert build.exact_link_count == 2

        repository = SymptomConceptRepository(engine)
        verifier = SelectNauseaVerifier()
        concept = await repository.resolve(
            symptom_text="속 울렁거림",
            pro_ctcae_source_version=pro_source,
            embedding_provider=provider,
            semantic_verifier=verifier,
            top_k=5,
            min_similarity=0.0,
        )

        assert concept is not None
        assert concept.symptom_term == "Nausea"
        assert concept.match_type == "VECTOR_LLM_VERIFIED"
        assert provider.queries
        assert verifier.calls[0][2] == "clinical_symptom_concept"

        linked = repository.linked_reaction_candidates(
            concept=concept,
            mfds_source_version=mfds_source,
        )
        assert {item.normalized_term for item in linked} == {
            "메스꺼움",
            "구역",
        }
        assert "속 부글거림" not in {
            item.normalized_term for item in linked
        }

        repository.remember(
            trace_id="trace-concept-1",
            symptom_text="속 울렁거림",
            concept=concept,
            ttl_seconds=3_600,
        )
        recalled = repository.recall(
            trace_id="trace-concept-1",
            symptom_text="속 울렁거림",
            pro_ctcae_source_version=pro_source,
        )
        assert recalled is not None
        assert recalled.concept_id == concept.concept_id
        assert recalled.symptom_term == concept.symptom_term
        assert recalled.match_type == concept.match_type
        assert recalled.similarity == concept.similarity

        questionnaire = pro_ctcae_result_for_concept(
            "속 울렁거림",
            recalled,
            workbook_path=workbook_path,
        )
        assert questionnaire.matched_symptom_term == "Nausea"
        assert questionnaire.questions
        assert questionnaire.scoring_method == "linked_concept_reuse"
        assert len(provider.queries) == 1

        semantic_build = await AgentSymptomConceptBackfill(
            engine,
            provider,
        ).semantic(
            workbook_path,
            semantic_verifier=SelectMfdsTermVerifier(),
            verifier_model="fixture-sonnet",
            limit=1,
            top_k=5,
            min_similarity=0.0,
        )
        assert semantic_build.semantic_concepts_processed == 1
        assert semantic_build.semantic_concepts_linked == 1
        assert len(provider.queries) == 1
        with engine.connect() as connection:
            status = connection.execute(
                text(
                    """
                    SELECT status, candidate_count, linked_term_count
                    FROM clinical_symptom_concept_link_statuses
                    WHERE concept_id = :concept_id
                    """
                ),
                {"concept_id": concept.concept_id},
            ).one()
        assert status[0] == "COMPLETED"
        assert status[1] >= 1
        assert status[2] == 1
    finally:
        cleanup()
