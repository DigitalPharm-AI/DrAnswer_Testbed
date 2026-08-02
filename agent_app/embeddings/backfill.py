from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from agent_app.ae_pro_ctcae import (
    load_workbook,
    normalize_pro_ctcae_lookup_key,
    pro_ctcae_source_version,
)
from agent_app.embeddings.base import EmbeddingProvider
from agent_app.embeddings.reference_text import (
    adverse_reaction_embedding_text,
    adverse_reaction_source_version,
    pro_ctcae_embedding_text,
)
from agent_app.persistence.adverse_reaction_repository import (
    normalize_reaction_lookup_key,
)


@dataclass(frozen=True)
class EmbeddingBackfillResult:
    target: str
    source_version: str
    total_source_rows: int
    pending_rows: int
    processed_rows: int
    skipped_rows: int
    api_batches: int
    source_characters: int
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentEmbeddingBackfill:
    """Resume-safe reference-data embedding backfill.

    Each committed batch is independently durable. A rerun selects only rows
    missing for the current model and source version, preventing duplicate
    Bedrock calls after interruption.
    """

    def __init__(
        self,
        engine: Engine,
        provider: EmbeddingProvider,
        *,
        batch_size: int = 96,
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("embedding_backfill_postgresql_required")
        if batch_size < 1 or batch_size > 96:
            raise ValueError("embedding_backfill_batch_size_invalid")
        self.engine = engine
        self.provider = provider
        self.batch_size = batch_size
        self.on_progress = on_progress

    async def adverse_reactions(
        self,
        *,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> EmbeddingBackfillResult:
        _validate_limit(limit)
        source_version = self._mfds_source_version()
        total_source_rows = self._scalar(
            "SELECT count(*) FROM mfds_adverse_reactions"
        )
        pending_rows = self._scalar(
            """
            SELECT count(*)
            FROM mfds_adverse_reactions AS reaction
            LEFT JOIN mfds_adverse_reaction_embeddings AS embedded
              ON embedded.reaction_id = reaction.reaction_id
             AND embedded.model_id = :model_id
             AND embedded.source_version = :source_version
            WHERE embedded.embedding_id IS NULL
            """,
            self._version_params(source_version),
        )
        target_rows = min(pending_rows, limit) if limit is not None else pending_rows
        source_characters = self._scalar(
            """
            SELECT COALESCE(sum(char_length(source.normalized_term)), 0)
            FROM (
                SELECT reaction.normalized_term
                FROM mfds_adverse_reactions AS reaction
                LEFT JOIN mfds_adverse_reaction_embeddings AS embedded
                  ON embedded.reaction_id = reaction.reaction_id
                 AND embedded.model_id = :model_id
                 AND embedded.source_version = :source_version
                WHERE embedded.embedding_id IS NULL
                ORDER BY reaction.reaction_id
                LIMIT :row_limit
            ) AS source
            """,
            {
                **self._version_params(source_version),
                "row_limit": target_rows,
            },
        )
        source_characters += target_rows * len(
            adverse_reaction_embedding_text("")
        )
        if dry_run or target_rows == 0:
            return _result(
                target="adverse_reactions",
                source_version=source_version,
                total=total_source_rows,
                pending=pending_rows,
                processed=0,
                characters=source_characters,
                batches=0,
                dry_run=dry_run,
            )

        processed = 0
        batches = 0
        while processed < target_rows:
            rows = self._pending_adverse_reactions(
                source_version,
                min(self.batch_size, target_rows - processed),
            )
            if not rows:
                break
            source_texts = [
                adverse_reaction_embedding_text(
                    str(row["normalized_term"])
                )
                for row in rows
            ]
            embeddings = await self.provider.embed_documents(source_texts)
            self._write_adverse_reactions(
                rows,
                embeddings,
                source_version=source_version,
            )
            processed += len(rows)
            batches += 1
            self._report_progress(
                "adverse_reactions",
                processed,
                target_rows,
            )

        return _result(
            target="adverse_reactions",
            source_version=source_version,
            total=total_source_rows,
            pending=pending_rows,
            processed=processed,
            characters=source_characters,
            batches=batches,
            dry_run=False,
        )

    async def pro_ctcae(
        self,
        workbook_path: Path,
        *,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> EmbeddingBackfillResult:
        _validate_limit(limit)
        path = Path(workbook_path).resolve()
        source_version = pro_ctcae_source_version(path)
        records = _pro_ctcae_alias_records(path)
        existing = self._existing_pro_ctcae_keys(source_version)
        pending = [
            record
            for record in records
            if _pro_ctcae_key(record) not in existing
        ]
        if limit is not None:
            pending = pending[:limit]
        source_characters = sum(
            len(pro_ctcae_embedding_text(row["alias_text"]))
            for row in pending
        )
        if dry_run or not pending:
            return _result(
                target="pro_ctcae",
                source_version=source_version,
                total=len(records),
                pending=len(records) - len(existing),
                processed=0,
                characters=source_characters,
                batches=0,
                dry_run=dry_run,
            )

        processed = 0
        batches = 0
        for offset in range(0, len(pending), self.batch_size):
            batch = pending[offset : offset + self.batch_size]
            embeddings = await self.provider.embed_documents(
                [
                    pro_ctcae_embedding_text(str(row["alias_text"]))
                    for row in batch
                ]
            )
            self._write_pro_ctcae(
                batch,
                embeddings,
                source_version=source_version,
            )
            processed += len(batch)
            batches += 1
            self._report_progress("pro_ctcae", processed, len(pending))

        return _result(
            target="pro_ctcae",
            source_version=source_version,
            total=len(records),
            pending=len(records) - len(existing),
            processed=processed,
            characters=source_characters,
            batches=batches,
            dry_run=False,
        )

    def _mfds_source_version(self) -> str:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT source_sha256, extractor_version
                        FROM mfds_import_runs
                        WHERE lower(status) = 'completed'
                        ORDER BY run_id DESC
                        LIMIT 1
                        """
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RuntimeError("mfds_embedding_source_unavailable")
        return adverse_reaction_source_version(
            str(row["source_sha256"]),
            str(row["extractor_version"]),
        )

    def _pending_adverse_reactions(
        self,
        source_version: str,
        row_limit: int,
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    text(
                        """
                        SELECT reaction.reaction_id,
                               reaction.normalized_term
                        FROM mfds_adverse_reactions AS reaction
                        LEFT JOIN mfds_adverse_reaction_embeddings AS embedded
                          ON embedded.reaction_id = reaction.reaction_id
                         AND embedded.model_id = :model_id
                         AND embedded.source_version = :source_version
                        WHERE embedded.embedding_id IS NULL
                        ORDER BY reaction.reaction_id
                        LIMIT :row_limit
                        """
                    ),
                    {
                        **self._version_params(source_version),
                        "row_limit": row_limit,
                    },
                ).mappings()
            )

    def _write_adverse_reactions(
        self,
        rows: list[dict[str, Any]],
        embeddings: list[list[float]],
        *,
        source_version: str,
    ) -> None:
        if len(rows) != len(embeddings):
            raise RuntimeError("adverse_embedding_count_mismatch")
        identity = self.provider.identity
        values = []
        for row, embedding in zip(rows, embeddings, strict=True):
            source_text = str(row["normalized_term"])
            values.append(
                {
                    "reaction_id": int(row["reaction_id"]),
                    "source_text": source_text,
                    "normalized_text": normalize_reaction_lookup_key(
                        source_text
                    ),
                    "embedding": _vector_literal(embedding),
                    "provider": identity.provider,
                    "model_id": identity.model_id,
                    "model_region": identity.region,
                    "dimensions": identity.dimensions,
                    "source_version": source_version,
                    "source_sha256": _text_sha256(source_text),
                }
            )
        with self.engine.begin() as connection:
            connection.execute(_ADVERSE_UPSERT, values)

    def _existing_pro_ctcae_keys(
        self,
        source_version: str,
    ) -> set[tuple[str, str, str]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT symptom_term, korean_symptom_name,
                           normalized_alias
                    FROM agent_pro_ctcae_alias_embeddings
                    WHERE model_id = :model_id
                      AND source_version = :source_version
                    """
                ),
                self._version_params(source_version),
            ).all()
        return {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in rows
        }

    def _write_pro_ctcae(
        self,
        rows: list[dict[str, str]],
        embeddings: list[list[float]],
        *,
        source_version: str,
    ) -> None:
        if len(rows) != len(embeddings):
            raise RuntimeError("pro_ctcae_embedding_count_mismatch")
        identity = self.provider.identity
        values = []
        for row, embedding in zip(rows, embeddings, strict=True):
            values.append(
                {
                    **row,
                    "embedding": _vector_literal(embedding),
                    "provider": identity.provider,
                    "model_id": identity.model_id,
                    "model_region": identity.region,
                    "dimensions": identity.dimensions,
                    "source_version": source_version,
                    "source_sha256": _text_sha256(row["alias_text"]),
                }
            )
        with self.engine.begin() as connection:
            connection.execute(_PRO_CTCAE_UPSERT, values)

    def _version_params(self, source_version: str) -> dict[str, str]:
        return {
            "model_id": self.provider.identity.model_id,
            "source_version": source_version,
        }

    def _report_progress(
        self,
        target: str,
        processed: int,
        total: int,
    ) -> None:
        if self.on_progress is not None:
            self.on_progress(target, processed, total)

    def _scalar(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(text(statement), params or {}).scalar_one()
            )


_ADVERSE_UPSERT = text(
    """
    INSERT INTO mfds_adverse_reaction_embeddings (
        reaction_id, source_text, normalized_text, embedding,
        provider, model_id, model_region, dimensions,
        source_version, source_sha256
    ) VALUES (
        :reaction_id, :source_text, :normalized_text,
        CAST(:embedding AS public.vector),
        :provider, :model_id, :model_region, :dimensions,
        :source_version, :source_sha256
    )
    ON CONFLICT (reaction_id, model_id, source_version)
    DO UPDATE SET
        source_text = EXCLUDED.source_text,
        normalized_text = EXCLUDED.normalized_text,
        embedding = EXCLUDED.embedding,
        provider = EXCLUDED.provider,
        model_region = EXCLUDED.model_region,
        dimensions = EXCLUDED.dimensions,
        source_sha256 = EXCLUDED.source_sha256,
        embedded_at = CURRENT_TIMESTAMP
    """
)

_PRO_CTCAE_UPSERT = text(
    """
    INSERT INTO agent_pro_ctcae_alias_embeddings (
        symptom_term, korean_symptom_name, sheet_name,
        alias_text, normalized_alias, embedding,
        provider, model_id, model_region, dimensions,
        source_version, source_sha256
    ) VALUES (
        :symptom_term, :korean_symptom_name, :sheet_name,
        :alias_text, :normalized_alias,
        CAST(:embedding AS public.vector),
        :provider, :model_id, :model_region, :dimensions,
        :source_version, :source_sha256
    )
    ON CONFLICT (
        symptom_term, korean_symptom_name, normalized_alias,
        model_id, source_version
    ) DO UPDATE SET
        sheet_name = EXCLUDED.sheet_name,
        alias_text = EXCLUDED.alias_text,
        embedding = EXCLUDED.embedding,
        provider = EXCLUDED.provider,
        model_region = EXCLUDED.model_region,
        dimensions = EXCLUDED.dimensions,
        source_sha256 = EXCLUDED.source_sha256,
        embedded_at = CURRENT_TIMESTAMP
    """
)


def _pro_ctcae_alias_records(path: Path) -> list[dict[str, str]]:
    workbook = load_workbook(path)
    records: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in workbook.parsed_entries:
        for alias in entry.aliases:
            normalized_alias = normalize_pro_ctcae_lookup_key(alias)
            if not normalized_alias:
                continue
            record = {
                "symptom_term": entry.symptom_term,
                "korean_symptom_name": entry.korean_symptom_name,
                "sheet_name": entry.sheet_name,
                "alias_text": alias.strip(),
                "normalized_alias": normalized_alias,
            }
            key = _pro_ctcae_key(record)
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
    return records


def _pro_ctcae_key(record: dict[str, str]) -> tuple[str, str, str]:
    return (
        record["symptom_term"],
        record["korean_symptom_name"],
        record["normalized_alias"],
    )


def _result(
    *,
    target: str,
    source_version: str,
    total: int,
    pending: int,
    processed: int,
    characters: int,
    batches: int,
    dry_run: bool,
) -> EmbeddingBackfillResult:
    return EmbeddingBackfillResult(
        target=target,
        source_version=source_version,
        total_source_rows=total,
        pending_rows=pending,
        processed_rows=processed,
        skipped_rows=max(total - pending, 0),
        api_batches=batches,
        source_characters=characters,
        dry_run=dry_run,
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _vector_literal(embedding: list[float]) -> str:
    return json.dumps(embedding, separators=(",", ":"))


def _validate_limit(limit: int | None) -> None:
    if limit is not None and limit < 1:
        raise ValueError("embedding_backfill_limit_must_be_positive")
