from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import anyio
from sqlalchemy import Engine, text

from agent_app.ae_pro_ctcae import normalize_pro_ctcae_lookup_key
from agent_app.embeddings.base import EmbeddingProvider
from agent_app.embeddings.reference_text import pro_ctcae_embedding_text
from agent_app.embeddings.semantic_verifier import (
    SemanticCandidate,
    SemanticMatchVerifier,
)
from agent_app.persistence.adverse_reaction_repository import (
    ReactionSemanticCandidate,
)
from shared.time_utils import utc_now


@dataclass(frozen=True)
class ClinicalSymptomConceptMatch:
    concept_id: int
    symptom_term: str
    korean_symptom_name: str
    sheet_name: str
    aliases: tuple[str, ...]
    match_type: str
    similarity: float
    pro_ctcae_source_version: str


@dataclass(frozen=True)
class ClinicalSymptomConceptResolution:
    status: Literal["MATCHED", "AMBIGUOUS", "NO_MATCH"]
    match: ClinicalSymptomConceptMatch | None
    candidates: tuple[ClinicalSymptomConceptMatch, ...]


class SymptomConceptRepository:
    """Resolve one clinical concept and reuse it across reference tools."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def resolve(
        self,
        *,
        symptom_text: str,
        pro_ctcae_source_version: str,
        embedding_provider: EmbeddingProvider,
        semantic_verifier: SemanticMatchVerifier,
        top_k: int,
        min_similarity: float,
    ) -> ClinicalSymptomConceptMatch | None:
        resolution = await self.resolve_with_status(
            symptom_text=symptom_text,
            pro_ctcae_source_version=pro_ctcae_source_version,
            embedding_provider=embedding_provider,
            semantic_verifier=semantic_verifier,
            top_k=top_k,
            min_similarity=min_similarity,
        )
        return resolution.match

    async def resolve_with_status(
        self,
        *,
        symptom_text: str,
        pro_ctcae_source_version: str,
        embedding_provider: EmbeddingProvider,
        semantic_verifier: SemanticMatchVerifier,
        top_k: int,
        min_similarity: float,
    ) -> ClinicalSymptomConceptResolution:
        clean_symptom = symptom_text.strip()
        normalized = normalize_pro_ctcae_lookup_key(clean_symptom)
        if not normalized:
            return ClinicalSymptomConceptResolution(
                status="NO_MATCH",
                match=None,
                candidates=(),
            )
        exact = await anyio.to_thread.run_sync(
            lambda: self._exact_match(
                normalized=normalized,
                pro_ctcae_source_version=pro_ctcae_source_version,
            )
        )
        if exact is not None:
            return ClinicalSymptomConceptResolution(
                status="MATCHED",
                match=exact,
                candidates=(exact,),
            )

        query_embedding = await embedding_provider.embed_query(
            pro_ctcae_embedding_text(clean_symptom)
        )
        vector_candidates = await anyio.to_thread.run_sync(
            lambda: self._vector_candidates(
                embedding=query_embedding,
                model_id=embedding_provider.identity.model_id,
                pro_ctcae_source_version=pro_ctcae_source_version,
                top_k=top_k,
                min_similarity=min_similarity,
            )
        )
        selected_ids = await semantic_verifier.matching_candidate_ids(
            clean_symptom,
            [
                SemanticCandidate(
                    candidate_id=str(candidate.concept_id),
                    label=(
                        f"{candidate.aliases[0]} | "
                        f"{candidate.korean_symptom_name} | "
                        f"{candidate.symptom_term}"
                    ),
                )
                for candidate in vector_candidates
            ],
            domain="clinical_symptom_concept",
        )
        selected = tuple(
            candidate
            for candidate in vector_candidates
            if str(candidate.concept_id) in selected_ids
        )
        if len(selected) == 1:
            return ClinicalSymptomConceptResolution(
                status="MATCHED",
                match=selected[0],
                candidates=selected,
            )
        if len(selected) > 1:
            return ClinicalSymptomConceptResolution(
                status="AMBIGUOUS",
                match=None,
                candidates=selected,
            )
        return ClinicalSymptomConceptResolution(
            status="NO_MATCH",
            match=None,
            candidates=tuple(vector_candidates),
        )

    def linked_reaction_candidates(
        self,
        *,
        concept: ClinicalSymptomConceptMatch,
        mfds_source_version: str,
    ) -> tuple[ReactionSemanticCandidate, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT link.reaction_id, reaction.normalized_term,
                           link.link_type, link.similarity
                    FROM mfds_reaction_concept_links AS link
                    JOIN mfds_adverse_reactions AS reaction
                      ON reaction.reaction_id = link.reaction_id
                    WHERE link.concept_id = :concept_id
                      AND link.verification_status = 'VERIFIED'
                      AND link.mfds_source_version = :mfds_source_version
                      AND link.pro_ctcae_source_version =
                          :pro_ctcae_source_version
                    ORDER BY link.similarity DESC, link.reaction_id
                    """
                ),
                {
                    "concept_id": concept.concept_id,
                    "mfds_source_version": mfds_source_version,
                    "pro_ctcae_source_version": (
                        concept.pro_ctcae_source_version
                    ),
                },
            ).mappings()
            return tuple(
                ReactionSemanticCandidate(
                    reaction_id=int(row["reaction_id"]),
                    normalized_term=str(row["normalized_term"]),
                    match_type=(
                        "concept_"
                        + str(row["link_type"]).strip().lower()
                    ),
                    similarity=float(row["similarity"]),
                )
                for row in rows
            )

    def remember(
        self,
        *,
        trace_id: str,
        symptom_text: str,
        concept: ClinicalSymptomConceptMatch,
        ttl_seconds: int,
    ) -> None:
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent_symptom_resolution_states (
                        trace_id, symptom_sha256, concept_id,
                        match_type, similarity,
                        pro_ctcae_source_version,
                        created_at, expires_at
                    ) VALUES (
                        :trace_id, :symptom_sha256, :concept_id,
                        :match_type, :similarity,
                        :pro_ctcae_source_version,
                        :created_at, :expires_at
                    )
                    ON CONFLICT (trace_id, symptom_sha256)
                    DO UPDATE SET
                        concept_id = EXCLUDED.concept_id,
                        match_type = EXCLUDED.match_type,
                        similarity = EXCLUDED.similarity,
                        pro_ctcae_source_version =
                            EXCLUDED.pro_ctcae_source_version,
                        created_at = EXCLUDED.created_at,
                        expires_at = EXCLUDED.expires_at
                    """
                ),
                {
                    "trace_id": trace_id,
                    "symptom_sha256": _symptom_hash(symptom_text),
                    "concept_id": concept.concept_id,
                    "match_type": concept.match_type,
                    "similarity": concept.similarity,
                    "pro_ctcae_source_version": (
                        concept.pro_ctcae_source_version
                    ),
                    "created_at": now,
                    "expires_at": expires_at,
                },
            )

    def recall(
        self,
        *,
        trace_id: str,
        symptom_text: str,
        pro_ctcae_source_version: str,
    ) -> ClinicalSymptomConceptMatch | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT concept.concept_id,
                               concept.symptom_term,
                               concept.korean_symptom_name,
                               concept.sheet_name,
                               state.match_type,
                               state.similarity,
                               state.pro_ctcae_source_version
                        FROM agent_symptom_resolution_states AS state
                        JOIN clinical_symptom_concepts AS concept
                          ON concept.concept_id = state.concept_id
                        WHERE state.trace_id = :trace_id
                          AND state.symptom_sha256 = :symptom_sha256
                          AND state.pro_ctcae_source_version =
                              :pro_ctcae_source_version
                          AND state.expires_at > :now
                        """
                    ),
                    {
                        "trace_id": trace_id,
                        "symptom_sha256": _symptom_hash(symptom_text),
                        "pro_ctcae_source_version": (
                            pro_ctcae_source_version
                        ),
                        "now": utc_now(),
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            aliases = self._aliases(
                connection,
                concept_id=int(row["concept_id"]),
                pro_ctcae_source_version=pro_ctcae_source_version,
            )
        return _concept_match(row, aliases=aliases)

    def mfds_source_version(self) -> str:
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
        from agent_app.embeddings.reference_text import (
            adverse_reaction_source_version,
        )

        return adverse_reaction_source_version(
            str(row["source_sha256"]),
            str(row["extractor_version"]),
        )

    def _exact_match(
        self,
        *,
        normalized: str,
        pro_ctcae_source_version: str,
    ) -> ClinicalSymptomConceptMatch | None:
        with self.engine.connect() as connection:
            rows = list(
                connection.execute(
                    text(
                        """
                        SELECT DISTINCT concept.concept_id,
                               concept.symptom_term,
                               concept.korean_symptom_name,
                               concept.sheet_name,
                               concept.pro_ctcae_source_version,
                               alias.alias_text
                        FROM clinical_symptom_aliases AS alias
                        JOIN clinical_symptom_concepts AS concept
                          ON concept.concept_id = alias.concept_id
                        WHERE alias.normalized_alias = :normalized
                          AND concept.pro_ctcae_source_version =
                              :pro_ctcae_source_version
                        ORDER BY concept.concept_id
                        """
                    ),
                    {
                        "normalized": normalized,
                        "pro_ctcae_source_version": (
                            pro_ctcae_source_version
                        ),
                    },
                ).mappings()
            )
            concept_ids = {int(row["concept_id"]) for row in rows}
            if len(concept_ids) != 1:
                return None
            row = rows[0]
            aliases = self._aliases(
                connection,
                concept_id=int(row["concept_id"]),
                pro_ctcae_source_version=pro_ctcae_source_version,
            )
        return ClinicalSymptomConceptMatch(
            concept_id=int(row["concept_id"]),
            symptom_term=str(row["symptom_term"]),
            korean_symptom_name=str(row["korean_symptom_name"]),
            sheet_name=str(row["sheet_name"]),
            aliases=aliases,
            match_type="EXACT",
            similarity=1.0,
            pro_ctcae_source_version=pro_ctcae_source_version,
        )

    def _vector_candidates(
        self,
        *,
        embedding: list[float],
        model_id: str,
        pro_ctcae_source_version: str,
        top_k: int,
        min_similarity: float,
    ) -> list[ClinicalSymptomConceptMatch]:
        with self.engine.connect() as connection:
            connection.execute(
                text("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            )
            raw_rows = list(
                connection.execute(
                    text(
                        """
                        SELECT concept.concept_id,
                               concept.symptom_term,
                               concept.korean_symptom_name,
                               concept.sheet_name,
                               concept.pro_ctcae_source_version,
                               embedded.alias_text,
                               1 - (
                                   embedded.embedding
                                   OPERATOR(public.<=>)
                                   CAST(:embedding AS public.vector)
                               ) AS similarity
                        FROM agent_pro_ctcae_alias_embeddings AS embedded
                        JOIN clinical_symptom_concepts AS concept
                          ON concept.symptom_term = embedded.symptom_term
                         AND concept.korean_symptom_name =
                             embedded.korean_symptom_name
                         AND concept.sheet_name = embedded.sheet_name
                         AND concept.pro_ctcae_source_version =
                             embedded.source_version
                        WHERE embedded.model_id = :model_id
                          AND embedded.source_version =
                              :pro_ctcae_source_version
                        ORDER BY embedded.embedding
                            OPERATOR(public.<=>)
                            CAST(:embedding AS public.vector)
                        LIMIT :candidate_limit
                        """
                    ),
                    {
                        "embedding": json.dumps(
                            embedding,
                            separators=(",", ":"),
                        ),
                        "model_id": model_id,
                        "pro_ctcae_source_version": (
                            pro_ctcae_source_version
                        ),
                        "candidate_limit": min(top_k * 4, 80),
                    },
                ).mappings()
            )
            candidates: list[ClinicalSymptomConceptMatch] = []
            seen: set[int] = set()
            for row in raw_rows:
                similarity = float(row["similarity"])
                concept_id = int(row["concept_id"])
                if similarity < min_similarity or concept_id in seen:
                    continue
                seen.add(concept_id)
                aliases = self._aliases(
                    connection,
                    concept_id=concept_id,
                    pro_ctcae_source_version=pro_ctcae_source_version,
                )
                ordered_aliases = tuple(
                    dict.fromkeys((str(row["alias_text"]), *aliases))
                )
                candidates.append(
                    ClinicalSymptomConceptMatch(
                        concept_id=concept_id,
                        symptom_term=str(row["symptom_term"]),
                        korean_symptom_name=str(
                            row["korean_symptom_name"]
                        ),
                        sheet_name=str(row["sheet_name"]),
                        aliases=ordered_aliases,
                        match_type="VECTOR_LLM_VERIFIED",
                        similarity=round(similarity, 6),
                        pro_ctcae_source_version=(
                            pro_ctcae_source_version
                        ),
                    )
                )
                if len(candidates) >= top_k:
                    break
        return candidates

    @staticmethod
    def _aliases(
        connection,
        *,
        concept_id: int,
        pro_ctcae_source_version: str,
    ) -> tuple[str, ...]:
        return tuple(
            str(value)
            for value in connection.execute(
                text(
                    """
                    SELECT alias_text
                    FROM clinical_symptom_aliases
                    WHERE concept_id = :concept_id
                      AND source_version = :source_version
                    ORDER BY
                        CASE source_type
                            WHEN 'PRO_CTCAE' THEN 0
                            WHEN 'CURATED' THEN 1
                            ELSE 2
                        END,
                        alias_id
                    """
                ),
                {
                    "concept_id": concept_id,
                    "source_version": pro_ctcae_source_version,
                },
            ).scalars()
        )


def _concept_match(
    row,
    *,
    aliases: tuple[str, ...],
) -> ClinicalSymptomConceptMatch:
    return ClinicalSymptomConceptMatch(
        concept_id=int(row["concept_id"]),
        symptom_term=str(row["symptom_term"]),
        korean_symptom_name=str(row["korean_symptom_name"]),
        sheet_name=str(row["sheet_name"]),
        aliases=aliases,
        match_type=str(row["match_type"]),
        similarity=float(row["similarity"]),
        pro_ctcae_source_version=str(
            row["pro_ctcae_source_version"]
        ),
    )


def _symptom_hash(symptom_text: str) -> str:
    normalized = normalize_pro_ctcae_lookup_key(symptom_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
