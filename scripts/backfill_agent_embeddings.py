from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agent_app.embeddings.backfill import AgentEmbeddingBackfill
from agent_app.embeddings.factory import get_embedding_provider
from agent_app.persistence.schema import verify_agent_schema_current
from shared.db import DatabaseEngineConfig, create_database_engine
from shared.settings import get_settings


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill Cohere embeddings into the Agent pgvector DB.",
    )
    parser.add_argument(
        "--target",
        choices=("adverse-reactions", "pro-ctcae", "all"),
        default="all",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "perform a dry run and exit non-zero when any current "
            "reference embedding is missing"
        ),
    )
    parser.add_argument("--workbook", type=Path)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> list[dict]:
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
        backfill = AgentEmbeddingBackfill(
            engine,
            get_embedding_provider(),
            batch_size=settings.embedding_batch_size,
            on_progress=_print_progress,
        )
        results = []
        if args.target in {"adverse-reactions", "all"}:
            results.append(
                (
                    await backfill.adverse_reactions(
                        limit=args.limit,
                        dry_run=args.dry_run or args.check,
                    )
                ).as_dict()
            )
        if args.target in {"pro-ctcae", "all"}:
            results.append(
                (
                    await backfill.pro_ctcae(
                        args.workbook or settings.pro_ctcae_workbook_path,
                        limit=args.limit,
                        dry_run=args.dry_run or args.check,
                    )
                ).as_dict()
            )
        return results
    finally:
        engine.dispose()


def _print_progress(target: str, processed: int, total: int) -> None:
    print(
        json.dumps(
            {
                "event": "embedding_backfill_progress",
                "target": target,
                "processed": processed,
                "total": total,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main() -> int:
    args = _parse_args()
    results = asyncio.run(_run(args))
    print(json.dumps(results, ensure_ascii=False))
    if args.check and any(
        int(result.get("pending_rows") or 0) > 0
        for result in results
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
