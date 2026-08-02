from __future__ import annotations

from agent_app.persistence.adverse_reaction_repository import (
    MedicationReactionLookup,
)


class TestbedAdverseReactionLookup:
    """Small deterministic fixture for tests that do not provision PostgreSQL."""

    _REACTIONS = {
        "메트포르민": "구역",
        "수니티닙": "구역",
        "암로디핀": "현기증",
        "레트로졸": "근육통",
    }

    def lookup_medication_reactions(
        self,
        *,
        medication_name: str,
        symptom_terms: list[str],
    ) -> MedicationReactionLookup:
        reaction = next(
            (value for key, value in self._REACTIONS.items() if key in medication_name),
            "",
        )
        normalized_terms = " ".join(symptom_terms)
        nausea_match = reaction == "구역" and any(token in normalized_terms for token in ("메스꺼", "울렁", "구역", "Nausea"))
        direct_match = reaction and reaction in normalized_terms
        matches = ()
        if reaction and (nausea_match or direct_match):
            matches = (
                {
                    "item_seq": f"fixture-{reaction}",
                    "item_name": medication_name,
                    "entp_name": "fixture",
                    "document_hash": "fixture-document",
                    "section_path": "사용상의주의사항/이상반응",
                    "section_heading": "이상반응",
                    "reaction_normalized": reaction,
                    "reaction_raw": reaction,
                    "frequency_text": "",
                    "assertion": "LISTED",
                    "evidence_text": f"{reaction}이 기재되어 있다.",
                    "extraction_method": "fixture",
                    "extractor_version": "fixture",
                    "confidence": 0.91,
                    "review_status": "CANDIDATE",
                },
            )
        return MedicationReactionLookup(
            medication_name=medication_name,
            medication_query=medication_name,
            resolution_method="fixture",
            products=(),
            matches=matches,
            source={
                "kind": "MFDS_LABEL_TRACE_DB",
                "review_status": "CANDIDATE_NOT_CLINICALLY_REVIEWED",
            },
        )
