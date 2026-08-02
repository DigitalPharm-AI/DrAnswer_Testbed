from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agent_app.embeddings.concept_backfill import (
    AgentSymptomConceptBackfill,
)
from agent_app.embeddings.factory import get_embedding_provider
from agent_app.embeddings.semantic_verifier import LlmSemanticMatchVerifier
from agent_app.persistence.schema import verify_agent_schema_current
from agent_app.providers.bedrock import BedrockAnthropicProvider
from shared.db import DatabaseEngineConfig, create_database_engine
from shared.settings import get_settings


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the versioned MFDS to Pro-CTCAE symptom concept bridge."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("exact", "semantic"),
        default="exact",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workbook", type=Path)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict:
    settings = get_settings()
    settings.require_agent_migration_postgresql()
    engine = create_database_engine(
        settings.agent_migration_database_url,
        config=DatabaseEngineConfig(
            pool_size=1,
            max_overflow=0,
            statement_timeout_ms=0,
            lock_timeout_ms=5_000,
        ),
    )
    try:
        verify_agent_schema_current(engine)
        embedding_provider = get_embedding_provider()
        backfill = AgentSymptomConceptBackfill(
            engine,
            embedding_provider,
        )
        workbook = args.workbook or settings.pro_ctcae_workbook_path
        if args.mode == "exact":
            result = backfill.exact(workbook)
        else:
            if args.limit is None:
                raise ValueError(
                    "--limit is required for semantic linking"
                )
            provider = BedrockAnthropicProvider()
            result = await backfill.semantic(
                workbook,
                semantic_verifier=LlmSemanticMatchVerifier(
                    provider,
                    model_factory=provider.reference_linking_model,
                ),
                verifier_model=settings.model_id_for_tier("sonnet"),
                limit=args.limit,
                top_k=(
                    settings.symptom_concept_mfds_candidate_top_k
                ),
                min_similarity=(
                    settings.reference_vector_min_similarity
                ),
            )
        return result.as_dict()
    finally:
        engine.dispose()


def main() -> int:
    args = _parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
