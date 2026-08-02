from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from agent_app.persistence.adverse_reaction_repository import (
    AgentAdverseReactionRepository,
)
from agent_app.persistence.db import engine
from agent_app.tools.mcp_server import AgentMcpToolServer

CASES: tuple[tuple[str, str, str, str], ...] = (
    ("메트포르민 500mg", "발진", "200401015", "발진"),
    ("암로디핀 5mg", "근육통", "200610660", "근육통"),
    ("수니티닙 50mg", "설사", "200606182", "설사"),
    ("레트로졸 2.5mg", "어지러워요", "200108765", "현기증"),
)

REFERENCE_TABLES = (
    "mfds_import_runs",
    "mfds_drug_products",
    "mfds_label_documents",
    "mfds_product_label_documents",
    "mfds_label_sections",
    "mfds_document_label_sections",
    "mfds_adverse_reactions",
    "mfds_section_adverse_reactions",
    "mfds_drug_aliases",
)


def main() -> int:
    repository = AgentAdverseReactionRepository(engine)
    server = AgentMcpToolServer(
        backend_queries=object(),  # This verification exercises no Backend read.
        backend_client=object(),  # This verification exercises no Backend write.
        adverse_reactions=repository,
    )
    results: list[dict[str, object]] = []
    database_summary: dict[str, object] = {}
    try:
        if not repository.is_ready():
            raise RuntimeError("mfds_adverse_reaction_reference_unavailable")
        with engine.connect() as connection:
            database_summary = {
                "database": connection.execute(text("SELECT current_database()")).scalar_one(),
                "products": connection.execute(text("SELECT COUNT(*) FROM mfds_drug_products")).scalar_one(),
                "reaction_mentions": connection.execute(text("SELECT COUNT(*) FROM mfds_section_adverse_reactions")).scalar_one(),
                "reaction_terms": connection.execute(text("SELECT COUNT(*) FROM mfds_adverse_reactions")).scalar_one(),
                "storage_bytes": sum(
                    int(
                        connection.execute(
                            text("SELECT pg_total_relation_size(:table)"),
                            {"table": table_name},
                        ).scalar_one()
                    )
                    for table_name in REFERENCE_TABLES
                ),
            }
        for medication_name, symptom_text, item_seq, reaction_term in CASES:
            tool_result = asyncio.run(
                server._get_medication_side_effect_assessment(
                    {
                        "symptom_text": symptom_text,
                        "medication_name": medication_name,
                    },
                    trace_id="trace-testbed-mfds-verification",
                    source_event_type="medication_agent",
                    payload={
                        "context": {
                            "trusted_patient_context": {
                                "availability": {"today_medication": "available"},
                                "active_medication_schedules": [{"medication_name": medication_name}],
                                "today_medication": {"dose_events": []},
                            }
                        }
                    },
                )
            )
            if tool_result.status != "success":
                raise RuntimeError(f"testbed_side_effect_tool_failed:{tool_result.error}")
            assessment = tool_result.response
            matched_item_sequences = {str(match.get("item_seq") or "") for match in assessment.get("reference_matches", [])}
            matched_reactions = [str(match.get("reaction") or "") for match in assessment.get("reference_matches", [])]
            passed = (
                assessment.get("suspected") is True
                and assessment.get("reference_source") == "MFDS_LABEL_TRACE_DB"
                and item_seq in matched_item_sequences
                and any(reaction_term in reaction for reaction in matched_reactions)
            )
            results.append(
                {
                    "medication_name": medication_name,
                    "symptom_text": symptom_text,
                    "expected_item_seq": item_seq,
                    "matched_item_sequences": sorted(matched_item_sequences),
                    "matched_reactions": matched_reactions,
                    "reference_source": assessment.get("reference_source"),
                    "passed": passed,
                }
            )
    finally:
        engine.dispose()
    passed_count = sum(1 for result in results if result["passed"])
    payload = {
        "ok": passed_count == len(CASES),
        "passed": passed_count,
        "total": len(CASES),
        "database_summary": database_summary,
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
