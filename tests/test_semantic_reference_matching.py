from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from agent_app.ae_pro_ctcae import (
    load_workbook,
    match_pro_ctcae_symptom_semantic,
    pro_ctcae_source_version,
)
from agent_app.embeddings.base import EmbeddingIdentity
from agent_app.embeddings.reference_text import (
    adverse_reaction_embedding_text,
    adverse_reaction_source_version,
    pro_ctcae_embedding_text,
)
from agent_app.embeddings.semantic_verifier import SemanticCandidate
from agent_app.persistence.adverse_reaction_repository import (
    AgentAdverseReactionRepository,
)
from agent_app.persistence.schema import migrate_agent_schema
from agent_app.tools.medication_side_effects import (
    assess_side_effect_from_snapshot_semantic,
)
from shared.settings import Settings
from tests.helpers import build_agent_engine

POSTGRES_CONFIGURED = bool(
    os.getenv("AGENT_POSTGRES_TEST_DATABASE_URL", "").strip()
)
VECTOR = "[1," + ",".join(["0"] * 1023) + "]"


class FakeQueryEmbeddingProvider:
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
        return [[1.0] + [0.0] * 1023 for _text in texts]


class SelectAllVerifier:
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
        return {candidate.candidate_id for candidate in candidates}


@pytest.mark.skipif(
    not POSTGRES_CONFIGURED,
    reason="AGENT_POSTGRES_TEST_DATABASE_URL is required",
)
@pytest.mark.asyncio
async def test_adverse_reaction_exact_then_vector_llm_then_medication_intersection():
    engine, cleanup = build_agent_engine("semantic_adverse")
    try:
        migrate_agent_schema(engine)
        source_sha = "a" * 64
        source_version = adverse_reaction_source_version(
            source_sha,
            "fixture-v1",
        )
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
                {"source_sha": source_sha},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_drug_products (
                        item_seq, item_name, item_eng_name, entp_name,
                        entp_eng_name, item_permit_date, etc_otc_code,
                        cancel_name, cancel_date, change_date, atc_code,
                        main_item_ingr, main_ingr_eng, material_name,
                        search_text
                    ) VALUES (
                        'drug-1', '시험약', '', '시험사', '', '', '',
                        '정상', '', '', '', '시험성분', '', '', '시험약'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_label_documents (
                        document_id, document_hash, document_type,
                        raw_xml_zlib, raw_xml_length, parse_status
                    ) VALUES (1, 'doc', 'NB_DOC_DATA', :raw, 1, 'PARSED')
                    """
                ),
                {"raw": b"x"},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_product_label_documents (
                        item_seq, document_id
                    ) VALUES ('drug-1', 1)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_label_sections (
                        section_id, section_type, section_text, section_hash
                    ) VALUES (1, 'ADVERSE_REACTIONS', '설사', 'section')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_document_label_sections (
                        document_id, section_id, heading,
                        section_path, section_order
                    ) VALUES (1, 1, '이상반응', '이상반응', 1)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_adverse_reactions (
                        reaction_id, normalized_term
                    ) VALUES
                        (1, '설사'),
                        (2, '구역'),
                        (3, '메스꺼움')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_section_adverse_reactions (
                        mention_id, section_id, reaction_normalized,
                        reaction_raw, assertion, evidence_start,
                        evidence_end, reaction_start, reaction_end,
                        extraction_method, extractor_version,
                        confidence, review_status
                    ) VALUES (
                        1, 1, '설사', '설사', 'LISTED', 0, 2, 0, 2,
                        'fixture', 'fixture-v1', 0.95, 'CANDIDATE'
                    ), (
                        2, 1, '구역', '구역', 'LISTED', 0, 2, 0, 2,
                        'fixture', 'fixture-v1', 0.95, 'CANDIDATE'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_drug_aliases (
                        alias_normalized, item_seq, source
                    ) VALUES ('시험약', 'drug-1', 'fixture')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_adverse_reaction_embeddings (
                        reaction_id, source_text, normalized_text,
                        embedding, provider, model_id, model_region,
                        dimensions, source_version, source_sha256
                    ) VALUES (
                        1, '설사', '설사', CAST(:embedding AS public.vector),
                        'fake_cohere', :model_id, 'ap-northeast-1',
                        1024, :source_version, :source_sha
                    ), (
                        2, '구역', '구역', CAST(:embedding AS public.vector),
                        'fake_cohere', :model_id, 'ap-northeast-1',
                        1024, :source_version, :source_sha
                    ), (
                        3, '메스꺼움', '메스꺼움',
                        CAST(:embedding AS public.vector),
                        'fake_cohere', :model_id, 'ap-northeast-1',
                        1024, :source_version, :source_sha
                    )
                    """
                ),
                {
                    "embedding": VECTOR,
                    "model_id": FakeQueryEmbeddingProvider.identity.model_id,
                    "source_version": source_version,
                    "source_sha": source_sha,
                },
            )

        repository = AgentAdverseReactionRepository(engine)
        provider = FakeQueryEmbeddingProvider()
        verifier = SelectAllVerifier()
        snapshot = {
            "active_medication_schedules": [
                {"medication_name": "시험약 5mg"}
            ]
        }

        exact = await assess_side_effect_from_snapshot_semantic(
            symptom_text="설사",
            patient_snapshot=snapshot,
            adverse_reactions=repository,
            embedding_provider=provider,
            semantic_verifier=verifier,
        )
        semantic = await assess_side_effect_from_snapshot_semantic(
            symptom_text="물이 섞인 묽은 변이 나와요",
            patient_snapshot=snapshot,
            adverse_reactions=repository,
            embedding_provider=provider,
            semantic_verifier=verifier,
            min_similarity=0.0,
        )
        synonym_expanded = await assess_side_effect_from_snapshot_semantic(
            symptom_text="메스꺼움",
            patient_snapshot=snapshot,
            adverse_reactions=repository,
            embedding_provider=provider,
            semantic_verifier=verifier,
        )

        assert exact.suspected is True
        assert exact.reference_matches[0]["match_type"] == "exact"
        assert provider.queries == [
            adverse_reaction_embedding_text(
                "물이 섞인 묽은 변이 나와요"
            )
        ]
        assert semantic.suspected is True
        assert semantic.matched_items == ["시험약 5mg"]
        assert semantic.reference_matches[0]["match_type"] == (
            "vector_llm_verified"
        )
        assert synonym_expanded.suspected is True
        assert synonym_expanded.reference_matches[0]["reaction"] == "구역"
        assert synonym_expanded.reference_matches[0]["match_type"] == (
            "pro_ctcae_alias_exact"
        )
        assert verifier.calls[0][2] == "mfds_adverse_reaction"
    finally:
        cleanup()


@pytest.mark.skipif(
    not POSTGRES_CONFIGURED,
    reason="AGENT_POSTGRES_TEST_DATABASE_URL is required",
)
@pytest.mark.asyncio
async def test_pro_ctcae_exact_then_vector_llm_uses_same_workbook_version():
    engine, cleanup = build_agent_engine("semantic_pro_ctcae")
    try:
        migrate_agent_schema(engine)
        path = Path(Settings().pro_ctcae_workbook_path).resolve()
        entry = next(
            item
            for item in load_workbook(path).parsed_entries
            if item.symptom_term == "Nausea"
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent_pro_ctcae_alias_embeddings (
                        symptom_term, korean_symptom_name, sheet_name,
                        alias_text, normalized_alias, embedding,
                        provider, model_id, model_region, dimensions,
                        source_version, source_sha256
                    ) VALUES (
                        :symptom_term, :korean_name, :sheet_name,
                        '메스꺼움', '메스꺼움',
                        CAST(:embedding AS public.vector),
                        'fake_cohere', :model_id, 'ap-northeast-1', 1024,
                        :source_version, :source_sha
                    )
                    """
                ),
                {
                    "symptom_term": entry.symptom_term,
                    "korean_name": entry.korean_symptom_name,
                    "sheet_name": entry.sheet_name,
                    "embedding": VECTOR,
                    "model_id": FakeQueryEmbeddingProvider.identity.model_id,
                    "source_version": pro_ctcae_source_version(path),
                    "source_sha": "b" * 64,
                },
            )

        provider = FakeQueryEmbeddingProvider()
        verifier = SelectAllVerifier()
        exact = await match_pro_ctcae_symptom_semantic(
            "메스꺼움",
            engine=engine,
            embedding_provider=provider,
            semantic_verifier=verifier,
            top_k=5,
            min_similarity=0.0,
            workbook_path=path,
        )
        semantic = await match_pro_ctcae_symptom_semantic(
            "속이 계속 울렁거려요",
            engine=engine,
            embedding_provider=provider,
            semantic_verifier=verifier,
            top_k=5,
            min_similarity=0.0,
            workbook_path=path,
        )

        assert exact.match_type == "exact"
        assert exact.matched_symptom_term == "Nausea"
        assert semantic.match_type == "vector_llm_verified"
        assert semantic.questions
        assert provider.queries == [
            pro_ctcae_embedding_text("속이 계속 울렁거려요")
        ]
        assert verifier.calls[0][2] == "pro_ctcae_symptom_alias"
    finally:
        cleanup()
