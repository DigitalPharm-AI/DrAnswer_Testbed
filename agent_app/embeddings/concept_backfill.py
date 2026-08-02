from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from agent_app.ae_pro_ctcae import pro_ctcae_source_version
from agent_app.embeddings.base import EmbeddingProvider
from agent_app.embeddings.reference_text import (
    adverse_reaction_source_version,
)
from agent_app.embeddings.semantic_verifier import (
    SemanticCandidate,
    SemanticMatchVerifier,
)


@dataclass(frozen=True)
class ConceptLinkBackfillResult:
    mode: str
    pro_ctcae_source_version: str
    mfds_source_version: str
    concept_count: int
    alias_count: int
    exact_link_count: int
    semantic_concepts_processed: int
    semantic_concepts_linked: int
    semantic_concepts_unmatched: int
    semantic_concepts_review_required: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentSymptomConceptBackfill:
    """Build a versioned MFDS ↔ Pro-CTCAE concept bridge."""

    def __init__(
        self,
        engine: Engine,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("symptom_concept_backfill_postgresql_required")
        self.engine = engine
        self.embedding_provider = embedding_provider

    def exact(self, workbook_path: Path) -> ConceptLinkBackfillResult:
        pro_source = pro_ctcae_source_version(
            Path(workbook_path).resolve()
        )
        mfds_source = self._mfds_source_version()
        model_id = self.embedding_provider.identity.model_id
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO clinical_symptom_concepts (
                        symptom_term, korean_symptom_name, sheet_name,
                        pro_ctcae_source_version
                    )
                    SELECT DISTINCT symptom_term, korean_symptom_name,
                           sheet_name, source_version
                    FROM agent_pro_ctcae_alias_embeddings
                    WHERE model_id = :model_id
                      AND source_version = :pro_source
                    ON CONFLICT (
                        symptom_term, korean_symptom_name, sheet_name,
                        pro_ctcae_source_version
                    ) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {"model_id": model_id, "pro_source": pro_source},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO clinical_symptom_aliases (
                        concept_id, alias_text, normalized_alias,
                        source_type, source_version
                    )
                    SELECT DISTINCT concept.concept_id,
                           embedded.alias_text,
                           embedded.normalized_alias,
                           'PRO_CTCAE', embedded.source_version
                    FROM agent_pro_ctcae_alias_embeddings AS embedded
                    JOIN clinical_symptom_concepts AS concept
                      ON concept.symptom_term = embedded.symptom_term
                     AND concept.korean_symptom_name =
                         embedded.korean_symptom_name
                     AND concept.sheet_name = embedded.sheet_name
                     AND concept.pro_ctcae_source_version =
                         embedded.source_version
                    WHERE embedded.model_id = :model_id
                      AND embedded.source_version = :pro_source
                    ON CONFLICT (
                        concept_id, normalized_alias,
                        source_type, source_version
                    ) DO UPDATE SET alias_text = EXCLUDED.alias_text
                    """
                ),
                {"model_id": model_id, "pro_source": pro_source},
            )
            connection.execute(
                text(
                    """
                    WITH unambiguous_aliases AS (
                        SELECT alias.normalized_alias,
                               min(alias.concept_id) AS concept_id
                        FROM clinical_symptom_aliases AS alias
                        JOIN clinical_symptom_concepts AS concept
                          ON concept.concept_id = alias.concept_id
                        WHERE alias.source_type = 'PRO_CTCAE'
                          AND alias.source_version = :pro_source
                          AND concept.pro_ctcae_source_version = :pro_source
                        GROUP BY alias.normalized_alias
                        HAVING count(DISTINCT alias.concept_id) = 1
                    )
                    INSERT INTO mfds_reaction_concept_links (
                        reaction_id, concept_id, link_type, similarity,
                        verifier_model, verification_status,
                        mfds_source_version, pro_ctcae_source_version
                    )
                    SELECT adverse.reaction_id, alias.concept_id,
                           'EXACT', 1.0, '', 'VERIFIED',
                           CAST(:mfds_source AS VARCHAR(160)),
                           CAST(:pro_source AS VARCHAR(160))
                    FROM mfds_adverse_reaction_embeddings AS adverse
                    JOIN unambiguous_aliases AS alias
                      ON alias.normalized_alias = adverse.normalized_text
                    WHERE adverse.model_id = :model_id
                      AND adverse.source_version = :mfds_source
                    ON CONFLICT (
                        reaction_id, concept_id,
                        mfds_source_version, pro_ctcae_source_version
                    ) DO UPDATE SET
                        link_type = EXCLUDED.link_type,
                        similarity = EXCLUDED.similarity,
                        verifier_model = EXCLUDED.verifier_model,
                        verification_status = EXCLUDED.verification_status,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "model_id": model_id,
                    "mfds_source": mfds_source,
                    "pro_source": pro_source,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO clinical_symptom_aliases (
                        concept_id, alias_text, normalized_alias,
                        source_type, source_version
                    )
                    SELECT link.concept_id,
                           min(adverse.source_text) AS source_text,
                           adverse.normalized_text,
                           'MFDS',
                           CAST(:pro_source AS VARCHAR(160))
                    FROM mfds_reaction_concept_links AS link
                    JOIN mfds_adverse_reaction_embeddings AS adverse
                      ON adverse.reaction_id = link.reaction_id
                     AND adverse.model_id = :model_id
                     AND adverse.source_version = :mfds_source
                    WHERE link.mfds_source_version = :mfds_source
                      AND link.pro_ctcae_source_version = :pro_source
                      AND link.verification_status = 'VERIFIED'
                    GROUP BY link.concept_id, adverse.normalized_text
                    ON CONFLICT (
                        concept_id, normalized_alias,
                        source_type, source_version
                    ) DO UPDATE SET alias_text = EXCLUDED.alias_text
                    """
                ),
                {
                    "model_id": model_id,
                    "mfds_source": mfds_source,
                    "pro_source": pro_source,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_reaction_concept_decisions (
                        normalized_text, status, link_type,
                        best_similarity, verifier_model,
                        candidate_concept_ids_json,
                        mfds_source_version, pro_ctcae_source_version
                    )
                    SELECT adverse.normalized_text, 'LINKED', 'EXACT',
                           1.0, '',
                           ('[' || min(link.concept_id)::TEXT || ']'),
                           CAST(:mfds_source AS VARCHAR(160)),
                           CAST(:pro_source AS VARCHAR(160))
                    FROM mfds_reaction_concept_links AS link
                    JOIN mfds_adverse_reaction_embeddings AS adverse
                      ON adverse.reaction_id = link.reaction_id
                     AND adverse.model_id = :model_id
                     AND adverse.source_version = :mfds_source
                    WHERE link.mfds_source_version = :mfds_source
                      AND link.pro_ctcae_source_version = :pro_source
                      AND link.link_type = 'EXACT'
                    GROUP BY adverse.normalized_text
                    ON CONFLICT (
                        normalized_text,
                        mfds_source_version, pro_ctcae_source_version
                    ) DO UPDATE SET
                        status = EXCLUDED.status,
                        link_type = EXCLUDED.link_type,
                        best_similarity = EXCLUDED.best_similarity,
                        verifier_model = EXCLUDED.verifier_model,
                        candidate_concept_ids_json =
                            EXCLUDED.candidate_concept_ids_json,
                        decided_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "model_id": model_id,
                    "mfds_source": mfds_source,
                    "pro_source": pro_source,
                },
            )
        return self._result(
            mode="exact",
            pro_source=pro_source,
            mfds_source=mfds_source,
        )

    async def semantic(
        self,
        workbook_path: Path,
        *,
        semantic_verifier: SemanticMatchVerifier,
        verifier_model: str,
        limit: int,
        top_k: int,
        min_similarity: float,
    ) -> ConceptLinkBackfillResult:
        if limit < 1:
            raise ValueError("symptom_concept_semantic_limit_required")
        exact_result = self.exact(workbook_path)
        pending = self._pending_semantic_concepts(
            mfds_source=exact_result.mfds_source_version,
            pro_source=exact_result.pro_ctcae_source_version,
            limit=limit,
        )
        linked = 0
        unmatched = 0
        for concept in pending:
            candidates = self._mfds_candidates_for_concept(
                concept_id=int(concept["concept_id"]),
                mfds_source=exact_result.mfds_source_version,
                pro_source=exact_result.pro_ctcae_source_version,
                top_k=top_k,
                min_similarity=min_similarity,
            )
            selected_ids = await semantic_verifier.matching_candidate_ids(
                (
                    f"{concept['korean_symptom_name']} | "
                    f"{concept['symptom_term']}"
                ),
                [
                    SemanticCandidate(
                        candidate_id=str(candidate["embedding_id"]),
                        label=str(candidate["source_text"]),
                    )
                    for candidate in candidates
                ],
                domain="pro_ctcae_concept_to_mfds_reference_link",
            )
            selected = [
                candidate
                for candidate in candidates
                if str(candidate["embedding_id"]) in selected_ids
            ]
            if selected:
                linked += 1
            else:
                unmatched += 1
            self._write_semantic_concept_links(
                concept_id=int(concept["concept_id"]),
                selected=selected,
                candidate_count=len(candidates),
                verifier_model=verifier_model,
                mfds_source=exact_result.mfds_source_version,
                pro_source=exact_result.pro_ctcae_source_version,
            )
        return self._result(
            mode="semantic",
            pro_source=exact_result.pro_ctcae_source_version,
            mfds_source=exact_result.mfds_source_version,
            semantic_concepts_processed=len(pending),
            semantic_concepts_linked=linked,
            semantic_concepts_unmatched=unmatched,
        )

    def _pending_semantic_concepts(
        self,
        *,
        mfds_source: str,
        pro_source: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    text(
                        """
                        SELECT concept.concept_id,
                               concept.symptom_term,
                               concept.korean_symptom_name
                        FROM clinical_symptom_concepts AS concept
                        LEFT JOIN clinical_symptom_concept_link_statuses
                            AS status
                          ON status.concept_id = concept.concept_id
                         AND status.mfds_source_version = :mfds_source
                         AND status.pro_ctcae_source_version = :pro_source
                        WHERE concept.pro_ctcae_source_version = :pro_source
                          AND status.status_id IS NULL
                        ORDER BY concept.concept_id
                        LIMIT :row_limit
                        """
                    ),
                    {
                        "mfds_source": mfds_source,
                        "pro_source": pro_source,
                        "row_limit": limit,
                    },
                ).mappings()
            )

    def _mfds_candidates_for_concept(
        self,
        *,
        concept_id: int,
        mfds_source: str,
        pro_source: str,
        top_k: int,
        min_similarity: float,
    ) -> list[dict[str, Any]]:
        raw_rows: list[dict[str, Any]] = []
        with self.engine.connect() as connection:
            alias_ids = list(
                connection.execute(
                    text(
                        """
                        SELECT embedded.embedding_id
                        FROM agent_pro_ctcae_alias_embeddings AS embedded
                        JOIN clinical_symptom_concepts AS concept
                          ON concept.symptom_term = embedded.symptom_term
                         AND concept.korean_symptom_name =
                             embedded.korean_symptom_name
                         AND concept.sheet_name = embedded.sheet_name
                         AND concept.pro_ctcae_source_version =
                             embedded.source_version
                        WHERE concept.concept_id = :concept_id
                          AND embedded.model_id = :model_id
                          AND embedded.source_version = :pro_source
                        ORDER BY embedded.embedding_id
                        """
                    ),
                    {
                        "concept_id": concept_id,
                        "model_id": self.embedding_provider.identity.model_id,
                        "pro_source": pro_source,
                    },
                ).scalars()
            )
            for alias_id in alias_ids:
                connection.execute(
                    text("SET LOCAL hnsw.iterative_scan = 'strict_order'")
                )
                raw_rows.extend(
                    dict(row)
                    for row in connection.execute(
                        text(
                            """
                            SELECT adverse.embedding_id,
                                   adverse.normalized_text,
                                   adverse.source_text,
                                   1 - (
                                       adverse.embedding
                                       OPERATOR(public.<=>)
                                       (
                                           SELECT alias.embedding
                                           FROM agent_pro_ctcae_alias_embeddings
                                               AS alias
                                           WHERE alias.embedding_id = :alias_id
                                             AND alias.model_id = :model_id
                                             AND alias.source_version =
                                                 :pro_source
                                       )
                                   ) AS similarity
                            FROM mfds_adverse_reaction_embeddings AS adverse
                            WHERE adverse.model_id = :model_id
                              AND adverse.source_version = :mfds_source
                            ORDER BY adverse.embedding
                                OPERATOR(public.<=>)
                                (
                                    SELECT alias.embedding
                                    FROM agent_pro_ctcae_alias_embeddings
                                        AS alias
                                    WHERE alias.embedding_id = :alias_id
                                      AND alias.model_id = :model_id
                                      AND alias.source_version = :pro_source
                                )
                            LIMIT :candidate_limit
                            """
                        ),
                        {
                            "alias_id": int(alias_id),
                            "model_id": (
                                self.embedding_provider.identity.model_id
                            ),
                            "mfds_source": mfds_source,
                            "pro_source": pro_source,
                            "candidate_limit": top_k,
                        },
                    ).mappings()
                )
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in sorted(
            raw_rows,
            key=lambda item: float(item["similarity"]),
            reverse=True,
        ):
            normalized_text = str(row["normalized_text"])
            similarity = float(row["similarity"])
            if normalized_text in seen or similarity < min_similarity:
                continue
            seen.add(normalized_text)
            candidates.append(row)
            if len(candidates) >= top_k:
                break
        return candidates

    def _write_semantic_concept_links(
        self,
        *,
        concept_id: int,
        selected: list[dict[str, Any]],
        candidate_count: int,
        verifier_model: str,
        mfds_source: str,
        pro_source: str,
    ) -> None:
        with self.engine.begin() as connection:
            for candidate in selected:
                connection.execute(
                    text(
                        """
                        INSERT INTO mfds_reaction_concept_links (
                            reaction_id, concept_id, link_type, similarity,
                            verifier_model, verification_status,
                            mfds_source_version,
                            pro_ctcae_source_version
                        )
                        SELECT adverse.reaction_id, :concept_id,
                               'VECTOR_LLM_VERIFIED', :similarity,
                               :verifier_model, 'VERIFIED',
                               CAST(:mfds_source AS VARCHAR(160)),
                               CAST(:pro_source AS VARCHAR(160))
                        FROM mfds_adverse_reaction_embeddings AS adverse
                        WHERE adverse.normalized_text = :normalized_text
                          AND adverse.model_id = :model_id
                          AND adverse.source_version = :mfds_source
                        ON CONFLICT (
                            reaction_id, concept_id,
                            mfds_source_version,
                            pro_ctcae_source_version
                        ) DO UPDATE SET
                            link_type = CASE
                                WHEN mfds_reaction_concept_links.link_type
                                    = 'EXACT'
                                THEN 'EXACT'
                                ELSE EXCLUDED.link_type
                            END,
                            similarity = GREATEST(
                                mfds_reaction_concept_links.similarity,
                                EXCLUDED.similarity
                            ),
                            verifier_model = CASE
                                WHEN mfds_reaction_concept_links.link_type
                                    = 'EXACT'
                                THEN mfds_reaction_concept_links.verifier_model
                                ELSE EXCLUDED.verifier_model
                            END,
                            verification_status =
                                EXCLUDED.verification_status,
                            updated_at = CURRENT_TIMESTAMP
                        """
                    ),
                    {
                        "concept_id": concept_id,
                        "similarity": float(candidate["similarity"]),
                        "verifier_model": verifier_model,
                        "mfds_source": mfds_source,
                        "pro_source": pro_source,
                        "normalized_text": str(
                            candidate["normalized_text"]
                        ),
                        "model_id": self.embedding_provider.identity.model_id,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO clinical_symptom_aliases (
                            concept_id, alias_text, normalized_alias,
                            source_type, source_version
                        ) VALUES (
                            :concept_id, :alias_text, :normalized_alias,
                            'MFDS', CAST(:pro_source AS VARCHAR(160))
                        )
                        ON CONFLICT (
                            concept_id, normalized_alias,
                            source_type, source_version
                        ) DO UPDATE SET alias_text = EXCLUDED.alias_text
                        """
                    ),
                    {
                        "concept_id": concept_id,
                        "alias_text": str(candidate["source_text"]),
                        "normalized_alias": str(
                            candidate["normalized_text"]
                        ),
                        "pro_source": pro_source,
                    },
                )
            connection.execute(
                text(
                    """
                    INSERT INTO clinical_symptom_concept_link_statuses (
                        concept_id, mfds_source_version,
                        pro_ctcae_source_version, status,
                        candidate_count, linked_term_count,
                        verifier_model
                    ) VALUES (
                        :concept_id,
                        CAST(:mfds_source AS VARCHAR(160)),
                        CAST(:pro_source AS VARCHAR(160)),
                        'COMPLETED', :candidate_count,
                        :linked_term_count, :verifier_model
                    )
                    ON CONFLICT (
                        concept_id, mfds_source_version,
                        pro_ctcae_source_version
                    ) DO UPDATE SET
                        status = EXCLUDED.status,
                        candidate_count = EXCLUDED.candidate_count,
                        linked_term_count = EXCLUDED.linked_term_count,
                        verifier_model = EXCLUDED.verifier_model,
                        processed_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "concept_id": concept_id,
                    "mfds_source": mfds_source,
                    "pro_source": pro_source,
                    "candidate_count": candidate_count,
                    "linked_term_count": len(selected),
                    "verifier_model": verifier_model,
                },
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

    def _result(
        self,
        *,
        mode: str,
        pro_source: str,
        mfds_source: str,
        semantic_concepts_processed: int = 0,
        semantic_concepts_linked: int = 0,
        semantic_concepts_unmatched: int = 0,
        semantic_concepts_review_required: int = 0,
    ) -> ConceptLinkBackfillResult:
        with self.engine.connect() as connection:
            concept_count = int(
                connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM clinical_symptom_concepts
                        WHERE pro_ctcae_source_version = :pro_source
                        """
                    ),
                    {"pro_source": pro_source},
                ).scalar_one()
            )
            alias_count = int(
                connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM clinical_symptom_aliases
                        WHERE source_version = :pro_source
                        """
                    ),
                    {"pro_source": pro_source},
                ).scalar_one()
            )
            exact_link_count = int(
                connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM mfds_reaction_concept_links
                        WHERE mfds_source_version = :mfds_source
                          AND pro_ctcae_source_version = :pro_source
                          AND link_type = 'EXACT'
                          AND verification_status = 'VERIFIED'
                        """
                    ),
                    {
                        "mfds_source": mfds_source,
                        "pro_source": pro_source,
                    },
                ).scalar_one()
            )
        return ConceptLinkBackfillResult(
            mode=mode,
            pro_ctcae_source_version=pro_source,
            mfds_source_version=mfds_source,
            concept_count=concept_count,
            alias_count=alias_count,
            exact_link_count=exact_link_count,
            semantic_concepts_processed=semantic_concepts_processed,
            semantic_concepts_linked=semantic_concepts_linked,
            semantic_concepts_unmatched=semantic_concepts_unmatched,
            semantic_concepts_review_required=(
                semantic_concepts_review_required
            ),
        )
