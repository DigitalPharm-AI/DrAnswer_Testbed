from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from agent_app.observability.evidence import (
    TraceEvidenceCipher,
    TraceEvidenceContext,
)
from agent_app.persistence.db import SessionLocal
from agent_app.persistence.models import AgentRunStep
from shared.json_utils import parse_json_object
from shared.settings import get_settings


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=(
            "Decrypt one Agent Trace's decision evidence for an "
            "authorized operator."
        )
    )
    parser.add_argument("trace_id")
    args = parser.parse_args()

    settings = get_settings()
    cipher = TraceEvidenceCipher.from_settings(settings)
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(AgentRunStep)
                .where(AgentRunStep.trace_id == args.trace_id)
                .order_by(AgentRunStep.sequence)
            )
        )
    if not rows:
        raise SystemExit("trace_not_found")

    payload = []
    for row in rows:
        evidence = None
        if row.evidence_ciphertext:
            evidence = cipher.decrypt_json(
                row.evidence_ciphertext,
                context=TraceEvidenceContext(
                    trace_id=row.trace_id,
                    observation_id=row.observation_id,
                    step_type=row.step_type,
                    sequence=row.sequence,
                    trace_attempt_number=(
                        row.trace_attempt_number
                    ),
                ),
            )
        payload.append(
            {
                "sequence": row.sequence,
                "attempt": row.trace_attempt_number,
                "step_type": row.step_type,
                "step_name": row.step_name,
                "status": row.status,
                "tool_name": row.tool_name,
                "latency_ms": row.latency_ms,
                "metadata": parse_json_object(
                    row.metadata_json
                ),
                "evidence": evidence,
            }
        )
    print(
        json.dumps(
            {
                "trace_id": args.trace_id,
                "steps": payload,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
