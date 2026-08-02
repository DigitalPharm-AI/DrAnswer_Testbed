from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from agent_app.persistence.adverse_reaction_repository import (
    AgentAdverseReactionRepository,
    normalize_medication_lookup_key,
)
from agent_app.persistence.schema import migrate_agent_schema
from agent_app.tools.medication_side_effects import (
    assess_side_effect_from_snapshot,
)
from tests.helpers import build_agent_engine

POSTGRES_CONFIGURED = bool(os.getenv("AGENT_POSTGRES_TEST_DATABASE_URL", "").strip())


def test_medication_lookup_key_removes_testbed_dose() -> None:
    assert normalize_medication_lookup_key("수니티닙 50mg") == "수니티닙"
    assert normalize_medication_lookup_key("레트로졸 2.5 mg") == "레트로졸"


@pytest.mark.skipif(
    not POSTGRES_CONFIGURED,
    reason="AGENT_POSTGRES_TEST_DATABASE_URL is required",
)
def test_agent_trace_database_drives_side_effect_assessment() -> None:
    engine, cleanup = build_agent_engine("mfds_reference")
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
                        'fixture-v1', '2026-08-02T00:00:00',
                        '2026-08-02T00:01:00', 'COMPLETED', '{}'
                    )
                    """
                ),
                {"source_sha256": "a" * 64},
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
                        '200606182', '수텐캡슐50밀리그램', '',
                        '한국화이자제약(주)', '', '', '전문의약품',
                        '정상', '', '', '', '수니티닙말산염', '', '',
                        '수텐 수니티닙'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_label_documents (
                        document_id, document_hash, document_type,
                        raw_xml_zlib, raw_xml_length, parse_status,
                        parse_error
                    ) VALUES (
                        1, 'fixture-document', 'NB_DOC_DATA',
                        :raw_xml, 1, 'PARSED', NULL
                    )
                    """
                ),
                {"raw_xml": b"x"},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_product_label_documents (
                        item_seq, document_id, change_date
                    ) VALUES ('200606182', 1, '2026-08-02')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_label_sections (
                        section_id, section_type, section_text, section_hash
                    ) VALUES (
                        1, 'ADVERSE_REACTIONS', '설사가 매우 흔함',
                        'fixture-section'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_document_label_sections (
                        document_id, section_id, heading,
                        section_path, section_order
                    ) VALUES (
                        1, 1, '이상반응', '사용상의주의사항/이상반응', 1
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_adverse_reactions (
                        reaction_id, normalized_term
                    ) VALUES (1, '설사')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_section_adverse_reactions (
                        mention_id, section_id, reaction_normalized,
                        reaction_raw, organ_system_text, frequency_text,
                        population_text, condition_text, assertion,
                        evidence_start, evidence_end, reaction_start,
                        reaction_end, extraction_method,
                        extractor_version, confidence, review_status
                    ) VALUES (
                        1, 1, '설사', '설사', '위장관', '매우 흔함',
                        NULL, NULL, 'LISTED', 0, 9, 0, 2,
                        'fixture', 'fixture-v1', 0.96, 'CANDIDATE'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO mfds_drug_aliases (
                        alias_normalized, item_seq, source
                    ) VALUES (
                        '수니티닙', '200606182', 'test'
                    )
                    """
                )
            )

        repository = AgentAdverseReactionRepository(engine)
        result = assess_side_effect_from_snapshot(
            symptom_text="설사를 했어요",
            patient_snapshot={"active_medication_schedules": [{"medication_name": "수니티닙 50mg"}]},
            adverse_reactions=repository,
        )

        assert result.suspected is True
        assert result.matched_items == ["수니티닙 50mg"]
        assert result.reference_source == "MFDS_LABEL_TRACE_DB"
        assert result.reference_matches[0]["item_seq"] == "200606182"
        assert result.reference_matches[0]["reaction"] == "설사"
    finally:
        cleanup()
