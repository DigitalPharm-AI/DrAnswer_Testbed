from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from agent_app.embeddings.backfill import AgentEmbeddingBackfill
from agent_app.embeddings.base import EmbeddingIdentity
from agent_app.persistence.schema import migrate_agent_schema
from shared.settings import Settings
from tests.helpers import build_agent_engine

POSTGRES_CONFIGURED = bool(
    os.getenv("AGENT_POSTGRES_TEST_DATABASE_URL", "").strip()
)


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.identity = EmbeddingIdentity(
            provider="fake",
            model_id="cohere.embed-multilingual-v3",
            region="ap-northeast-1",
            dimensions=1024,
        )
        self.document_calls: list[list[str]] = []

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [
            [float(index + 1)] + [0.0] * 1023
            for index, _value in enumerate(texts)
        ]

    async def embed_query(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 1023


@pytest.mark.skipif(
    not POSTGRES_CONFIGURED,
    reason="AGENT_POSTGRES_TEST_DATABASE_URL is required",
)
@pytest.mark.asyncio
async def test_adverse_reaction_backfill_is_dry_run_safe_and_resumable():
    engine, cleanup = build_agent_engine("embedding_backfill")
    try:
        migrate_agent_schema(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_import_runs (
                        run_id, source_file, source_sha256,
                        extractor_version, started_at, completed_at,
                        status, stats_json
                    ) VALUES (
                        1, 'fixture.sqlite', :source_sha256,
                        'fixture-v1', CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, 'COMPLETED', '{}'
                    )
                    """
                ),
                {"source_sha256": "a" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_adverse_reactions (
                        reaction_id, normalized_term
                    ) VALUES (1, '구역'), (2, '현기증')
                    """
                )
            )

        provider = FakeEmbeddingProvider()
        backfill = AgentEmbeddingBackfill(
            engine,
            provider,
            batch_size=1,
        )

        preview = await backfill.adverse_reactions(dry_run=True)
        first = await backfill.adverse_reactions(limit=1)
        second = await backfill.adverse_reactions()
        completed = await backfill.adverse_reactions()

        assert preview.pending_rows == 2
        assert preview.processed_rows == 0
        assert first.processed_rows == 1
        assert second.processed_rows == 1
        assert completed.pending_rows == 0
        assert provider.document_calls == [
            ["의약품 부작용 증상 용어: 구역"],
            ["의약품 부작용 증상 용어: 현기증"],
        ]
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    """
                    SELECT normalized_text, public.vector_dims(embedding)
                    FROM mfds_adverse_reaction_embeddings
                    ORDER BY reaction_id
                    """
                )
            ).all()
        assert stored == [("구역", 1024), ("현기증", 1024)]
    finally:
        cleanup()


@pytest.mark.skipif(
    not POSTGRES_CONFIGURED,
    reason="AGENT_POSTGRES_TEST_DATABASE_URL is required",
)
@pytest.mark.asyncio
async def test_pro_ctcae_alias_backfill_uses_workbook_aliases():
    engine, cleanup = build_agent_engine("pro_ctcae_backfill")
    try:
        migrate_agent_schema(engine)
        provider = FakeEmbeddingProvider()
        backfill = AgentEmbeddingBackfill(engine, provider, batch_size=2)
        workbook_path = Path(Settings().pro_ctcae_workbook_path)

        preview = await backfill.pro_ctcae(
            workbook_path,
            limit=2,
            dry_run=True,
        )
        result = await backfill.pro_ctcae(workbook_path, limit=2)

        assert preview.processed_rows == 0
        assert preview.pending_rows >= 2
        assert result.processed_rows == 2
        assert len(provider.document_calls) == 1
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    """
                    SELECT count(*), min(public.vector_dims(embedding))
                    FROM agent_pro_ctcae_alias_embeddings
                    """
                )
            ).one()
        assert stored == (2, 1024)
    finally:
        cleanup()
