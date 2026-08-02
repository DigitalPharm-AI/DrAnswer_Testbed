from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from sqlalchemy import Engine, bindparam, text

from agent_app.embeddings.base import EmbeddingProvider
from agent_app.embeddings.reference_text import (
    adverse_reaction_embedding_text,
    adverse_reaction_source_version,
)
from agent_app.embeddings.semantic_verifier import (
    SemanticCandidate,
    SemanticMatchVerifier,
)

_DOSE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|%)\b",
    flags=re.IGNORECASE,
)
_NON_LOOKUP_CHARACTERS = re.compile(r"[^0-9a-z가-힣]+", flags=re.IGNORECASE)
_FORM_TOKENS = {"정", "캡슐", "주", "주사", "시럽", "액", "tablet", "capsule"}


def normalize_medication_lookup_key(value: str) -> str:
    normalized = _DOSE_PATTERN.sub(" ", (value or "").strip().lower())
    normalized = re.sub(r"\([^)]*\)", " ", normalized)
    tokens = [token for token in _NON_LOOKUP_CHARACTERS.sub(" ", normalized).split() if token and token not in _FORM_TOKENS]
    return " ".join(tokens)


def normalize_reaction_lookup_key(value: str) -> str:
    return _NON_LOOKUP_CHARACTERS.sub("", (value or "").lower())


@dataclass(frozen=True)
class MedicationReactionLookup:
    medication_name: str
    medication_query: str
    resolution_method: str
    products: tuple[dict[str, Any], ...]
    matches: tuple[dict[str, Any], ...]
    source: dict[str, Any]


@dataclass(frozen=True)
class ReactionSemanticCandidate:
    reaction_id: int
    normalized_term: str
    match_type: str
    similarity: float


class AdverseReactionLookup(Protocol):
    def lookup_medication_reactions(
        self,
        *,
        medication_name: str,
        symptom_terms: list[str],
    ) -> MedicationReactionLookup: ...


class AgentAdverseReactionRepository:
    """Read MFDS label-derived adverse reactions from the Agent/Trace DB."""

    def __init__(
        self,
        engine: Engine,
        *,
        min_confidence: float = 0.84,
        max_term_length: int = 60,
        max_products: int = 20,
        max_matches: int = 8,
    ) -> None:
        self.engine = engine
        self.min_confidence = min_confidence
        self.max_term_length = max_term_length
        self.max_products = max_products
        self.max_matches = max_matches

    def is_ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                return (
                    connection.execute(
                        text(
                            """
                            SELECT 1
                            FROM mfds_import_runs
                            WHERE lower(status) = 'completed'
                            ORDER BY run_id DESC
                            LIMIT 1
                            """
                        )
                    ).first()
                    is not None
                )
        except Exception:
            return False

    def lookup_medication_reactions(
        self,
        *,
        medication_name: str,
        symptom_terms: list[str],
    ) -> MedicationReactionLookup:
        medication_query = normalize_medication_lookup_key(medication_name)
        if not medication_query:
            return MedicationReactionLookup(
                medication_name=medication_name,
                medication_query="",
                resolution_method="unresolved",
                products=(),
                matches=(),
                source={},
            )
        normalized_terms = tuple(dict.fromkeys(key for value in symptom_terms if (key := normalize_reaction_lookup_key(value))))
        with self.engine.connect() as connection:
            source = self._source_metadata(connection)
            products, resolution_method = self._resolve_products(
                connection,
                medication_query=medication_query,
            )
            if not products or not normalized_terms:
                return MedicationReactionLookup(
                    medication_name=medication_name,
                    medication_query=medication_query,
                    resolution_method=resolution_method,
                    products=tuple(products),
                    matches=(),
                    source=source,
                )
            item_sequences = [str(row["item_seq"]) for row in products]
            rows = connection.execute(
                text(
                    """
                    SELECT item_seq, item_name, entp_name, document_hash,
                           section_path, section_heading,
                           reaction_normalized, reaction_raw,
                           organ_system_text, frequency_text,
                           population_text, condition_text, assertion,
                           evidence_text, extraction_method,
                           extractor_version, confidence, review_status
                    FROM mfds_product_adverse_reaction_view
                    WHERE item_seq IN :item_sequences
                      AND assertion <> 'NEGATED'
                      AND confidence >= :min_confidence
                      AND char_length(reaction_normalized)
                          <= :max_term_length
                      AND position(':' in reaction_normalized) = 0
                      AND reaction_normalized NOT LIKE '%임상시험%'
                      AND reaction_normalized NOT LIKE '%위약%'
                      AND reaction_normalized NOT LIKE '%환자%'
                      AND reaction_normalized NOT LIKE '%대상%'
                    ORDER BY confidence DESC, item_seq, reaction_id
                    LIMIT 5000
                    """
                ).bindparams(bindparam("item_sequences", expanding=True)),
                {
                    "item_sequences": item_sequences,
                    "min_confidence": self.min_confidence,
                    "max_term_length": self.max_term_length,
                },
            ).mappings()
            matches = self._matching_rows(rows, normalized_terms)
        return MedicationReactionLookup(
            medication_name=medication_name,
            medication_query=medication_query,
            resolution_method=resolution_method,
            products=tuple(products),
            matches=tuple(matches),
            source=source,
        )

    async def resolve_reaction_candidates(
        self,
        *,
        symptom_text: str,
        embedding_provider: EmbeddingProvider,
        semantic_verifier: SemanticMatchVerifier,
        top_k: int,
        min_similarity: float,
    ) -> tuple[ReactionSemanticCandidate, ...]:
        clean_symptom = symptom_text.strip()
        normalized = normalize_reaction_lookup_key(clean_symptom)
        if not normalized:
            return ()
        source_version = await anyio.to_thread.run_sync(
            self._embedding_source_version
        )
        exact = await anyio.to_thread.run_sync(
            lambda: self._exact_reaction_candidates(
                normalized=normalized,
                model_id=embedding_provider.identity.model_id,
                source_version=source_version,
            )
        )
        if exact:
            return tuple(exact)

        query_embedding = await embedding_provider.embed_query(
            adverse_reaction_embedding_text(clean_symptom)
        )
        vector_candidates = await anyio.to_thread.run_sync(
            lambda: self._vector_reaction_candidates(
                embedding=query_embedding,
                model_id=embedding_provider.identity.model_id,
                source_version=source_version,
                top_k=top_k,
                min_similarity=min_similarity,
            )
        )
        selected_ids = await semantic_verifier.matching_candidate_ids(
            clean_symptom,
            [
                SemanticCandidate(
                    candidate_id=str(candidate.reaction_id),
                    label=candidate.normalized_term,
                )
                for candidate in vector_candidates
            ],
            domain="mfds_adverse_reaction",
        )
        return tuple(
            candidate
            for candidate in vector_candidates
            if str(candidate.reaction_id) in selected_ids
        )

    def expand_reaction_candidates_by_exact_terms(
        self,
        *,
        candidates: tuple[ReactionSemanticCandidate, ...],
        equivalent_terms: list[str],
        model_id: str,
    ) -> tuple[ReactionSemanticCandidate, ...]:
        """Expand a verified concept to exact MFDS synonym reaction IDs."""

        normalized_terms = sorted(
            {
                normalize_reaction_lookup_key(term)
                for term in equivalent_terms
                if normalize_reaction_lookup_key(term)
            }
        )
        if not candidates or not normalized_terms:
            return candidates
        source_version = self._embedding_source_version()
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT reaction_id, source_text
                    FROM mfds_adverse_reaction_embeddings
                    WHERE normalized_text IN :normalized_terms
                      AND model_id = :model_id
                      AND source_version = :source_version
                    ORDER BY reaction_id
                    """
                ).bindparams(
                    bindparam("normalized_terms", expanding=True)
                ),
                {
                    "normalized_terms": normalized_terms,
                    "model_id": model_id,
                    "source_version": source_version,
                },
            ).mappings()
            exact_alias_rows = list(rows)

        expanded = list(candidates)
        seen_ids = {candidate.reaction_id for candidate in candidates}
        verified_similarity = max(
            candidate.similarity for candidate in candidates
        )
        for row in exact_alias_rows:
            reaction_id = int(row["reaction_id"])
            if reaction_id in seen_ids:
                continue
            seen_ids.add(reaction_id)
            expanded.append(
                ReactionSemanticCandidate(
                    reaction_id=reaction_id,
                    normalized_term=str(row["source_text"]),
                    match_type="pro_ctcae_alias_exact",
                    similarity=verified_similarity,
                )
            )
        return tuple(expanded)

    def lookup_medication_reactions_for_candidates(
        self,
        *,
        medication_name: str,
        candidates: tuple[ReactionSemanticCandidate, ...],
    ) -> MedicationReactionLookup:
        medication_query = normalize_medication_lookup_key(medication_name)
        if not medication_query:
            return MedicationReactionLookup(
                medication_name=medication_name,
                medication_query="",
                resolution_method="unresolved",
                products=(),
                matches=(),
                source={},
            )
        with self.engine.connect() as connection:
            source = self._source_metadata(connection)
            products, resolution_method = self._resolve_products(
                connection,
                medication_query=medication_query,
            )
            if not products or not candidates:
                return MedicationReactionLookup(
                    medication_name=medication_name,
                    medication_query=medication_query,
                    resolution_method=resolution_method,
                    products=tuple(products),
                    matches=(),
                    source=source,
                )
            candidate_by_id = {
                candidate.reaction_id: candidate
                for candidate in candidates
            }
            rows = connection.execute(
                text(
                    """
                    SELECT item_seq, item_name, entp_name, document_hash,
                           section_path, section_heading, reaction_id,
                           reaction_normalized, reaction_raw,
                           organ_system_text, frequency_text,
                           population_text, condition_text, assertion,
                           evidence_text, extraction_method,
                           extractor_version, confidence, review_status
                    FROM mfds_product_adverse_reaction_view
                    WHERE item_seq IN :item_sequences
                      AND reaction_id IN :reaction_ids
                      AND assertion <> 'NEGATED'
                      AND confidence >= :min_confidence
                      AND char_length(reaction_normalized)
                          <= :max_term_length
                    ORDER BY confidence DESC, item_seq, reaction_id
                    LIMIT 5000
                    """
                ).bindparams(
                    bindparam("item_sequences", expanding=True),
                    bindparam("reaction_ids", expanding=True),
                ),
                {
                    "item_sequences": [
                        str(row["item_seq"]) for row in products
                    ],
                    "reaction_ids": list(candidate_by_id),
                    "min_confidence": self.min_confidence,
                    "max_term_length": self.max_term_length,
                },
            ).mappings()
            matches = []
            seen: set[tuple[str, int]] = set()
            for row in rows:
                reaction_id = int(row["reaction_id"])
                key = (str(row["item_seq"]), reaction_id)
                if key in seen:
                    continue
                seen.add(key)
                candidate = candidate_by_id[reaction_id]
                match = dict(row)
                match["semantic_match_type"] = candidate.match_type
                match["semantic_similarity"] = candidate.similarity
                matches.append(match)
                if len(matches) >= self.max_matches:
                    break
        return MedicationReactionLookup(
            medication_name=medication_name,
            medication_query=medication_query,
            resolution_method=resolution_method,
            products=tuple(products),
            matches=tuple(matches),
            source=source,
        )

    def _embedding_source_version(self) -> str:
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
            raise RuntimeError("mfds_adverse_reaction_reference_unavailable")
        return adverse_reaction_source_version(
            str(row["source_sha256"]),
            str(row["extractor_version"]),
        )

    def _exact_reaction_candidates(
        self,
        *,
        normalized: str,
        model_id: str,
        source_version: str,
    ) -> list[ReactionSemanticCandidate]:
        with self.engine.connect() as connection:
            connection.execute(
                text("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            )
            rows = connection.execute(
                text(
                    """
                    SELECT reaction_id, source_text
                    FROM mfds_adverse_reaction_embeddings
                    WHERE normalized_text = :normalized
                      AND model_id = :model_id
                      AND source_version = :source_version
                    ORDER BY reaction_id
                    """
                ),
                {
                    "normalized": normalized,
                    "model_id": model_id,
                    "source_version": source_version,
                },
            ).mappings()
            return [
                ReactionSemanticCandidate(
                    reaction_id=int(row["reaction_id"]),
                    normalized_term=str(row["source_text"]),
                    match_type="exact",
                    similarity=1.0,
                )
                for row in rows
            ]

    def _vector_reaction_candidates(
        self,
        *,
        embedding: list[float],
        model_id: str,
        source_version: str,
        top_k: int,
        min_similarity: float,
    ) -> list[ReactionSemanticCandidate]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT reaction_id, source_text,
                           1 - (
                               embedding OPERATOR(public.<=>)
                               CAST(:embedding AS public.vector)
                           ) AS similarity
                    FROM mfds_adverse_reaction_embeddings
                    WHERE model_id = :model_id
                      AND source_version = :source_version
                    ORDER BY embedding OPERATOR(public.<=>)
                        CAST(:embedding AS public.vector)
                    LIMIT :top_k
                    """
                ),
                {
                    "embedding": json.dumps(
                        embedding,
                        separators=(",", ":"),
                    ),
                    "model_id": model_id,
                    "source_version": source_version,
                    "top_k": top_k,
                },
            ).mappings()
            return [
                ReactionSemanticCandidate(
                    reaction_id=int(row["reaction_id"]),
                    normalized_term=str(row["source_text"]),
                    match_type="vector_llm_verified",
                    similarity=round(float(row["similarity"]), 6),
                )
                for row in rows
                if float(row["similarity"]) >= min_similarity
            ]

    def _source_metadata(self, connection: Any) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    """
                SELECT source_sha256, extractor_version, completed_at, status
                FROM mfds_import_runs
                ORDER BY run_id DESC
                LIMIT 1
                """
                )
            )
            .mappings()
            .first()
        )
        if row is None or str(row["status"]).lower() != "completed":
            raise RuntimeError("mfds_adverse_reaction_reference_unavailable")
        return {
            "kind": "MFDS_LABEL_TRACE_DB",
            "source_sha256": row["source_sha256"],
            "extractor_version": row["extractor_version"],
            "completed_at": row["completed_at"],
            "review_status": "CANDIDATE_NOT_CLINICALLY_REVIEWED",
        }

    def _resolve_products(
        self,
        connection: Any,
        *,
        medication_query: str,
    ) -> tuple[list[dict[str, Any]], str]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT product.item_seq, product.item_name,
                       product.entp_name, product.main_item_ingr,
                       product.cancel_name
                FROM mfds_drug_aliases alias
                JOIN mfds_drug_products product
                  ON product.item_seq = alias.item_seq
                WHERE alias.alias_normalized = :query
                  AND product.cancel_name = '정상'
                ORDER BY product.item_seq
                LIMIT :limit
                """
                ),
                {"query": medication_query, "limit": self.max_products},
            )
            .mappings()
            .all()
        )
        if rows:
            return [dict(row) for row in rows], "alias_exact"

        rows = (
            connection.execute(
                text(
                    """
                SELECT item_seq, item_name, entp_name, main_item_ingr,
                       cancel_name
                FROM mfds_drug_products
                WHERE cancel_name = '정상'
                  AND (
                      item_seq = :query
                      OR search_text LIKE :pattern
                  )
                  AND main_item_ingr NOT LIKE '%|%'
                ORDER BY
                    CASE WHEN item_seq = :query THEN 0 ELSE 1 END,
                    item_name,
                    item_seq
                LIMIT :limit
                """
                ),
                {
                    "query": medication_query,
                    "pattern": f"%{medication_query}%",
                    "limit": self.max_products,
                },
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows], ("single_ingredient_search" if rows else "unresolved")

    def _matching_rows(
        self,
        rows: Any,
        symptom_terms: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            reaction_key = normalize_reaction_lookup_key(str(row["reaction_normalized"] or ""))
            if not reaction_key:
                continue
            if not any(term in reaction_key or reaction_key in term for term in symptom_terms if len(term) >= 2):
                continue
            deduplication_key = (
                str(row["item_seq"]),
                str(row["reaction_normalized"]),
            )
            if deduplication_key in seen:
                continue
            seen.add(deduplication_key)
            matches.append(dict(row))
            if len(matches) >= self.max_matches:
                break
        return matches
