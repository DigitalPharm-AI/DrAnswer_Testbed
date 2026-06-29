from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.redaction import redact_inline_secrets, stable_hash

CHANGE_LOG_PATH = Path("data/governance/agent_change_log.json")
VALID_CHANGE_TYPES = {"prompt", "model", "tool"}


def governance_change_log_summary(*, limit: int = 8) -> dict[str, Any]:
    entries = _load_entries()
    recent = list(reversed(entries[-limit:]))
    type_counts = Counter(str(entry.get("change_type") or "unknown") for entry in entries)
    return {
        "path": str(CHANGE_LOG_PATH),
        "count": len(entries),
        "type_counts": [{"label": key, "value": value} for key, value in sorted(type_counts.items())],
        "recent": [_entry_view(entry) for entry in recent],
    }


def append_governance_change(
    *,
    change_type: str,
    target: str,
    summary: str,
    owner: str = "",
    rollback: str = "",
    evidence: str = "",
) -> dict[str, Any]:
    safe_type = str(change_type or "").strip().lower()
    if safe_type not in VALID_CHANGE_TYPES:
        return {"success": False, "action": "change_log", "message": f"invalid change type: {safe_type or '-'}"}
    safe_target = redact_inline_secrets(str(target or "").strip(), limit=160)
    safe_summary = redact_inline_secrets(str(summary or "").strip(), limit=400)
    if not safe_target or not safe_summary:
        return {"success": False, "action": "change_log", "message": "change target and summary are required"}

    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "id": f"chg-{stable_hash(f'{now}:{safe_type}:{safe_target}:{safe_summary}')[:12]}",
        "change_type": safe_type,
        "target": safe_target,
        "summary": safe_summary,
        "owner": redact_inline_secrets(owner or _default_owner(safe_type), limit=120),
        "rollback": redact_inline_secrets(
            rollback or "Rollback to the last known-good prompt/model/tool configuration.",
            limit=300,
        ),
        "evidence": redact_inline_secrets(evidence or "LOGS operator entry", limit=300),
        "approval_status": "recorded",
        "created_at": now,
        "source": "LOGS",
    }
    entries = _load_entries()
    entries.append(entry)
    _write_entries(entries)
    return {
        "success": True,
        "action": "change_log",
        "entry_id": entry["id"],
        "message": "prompt/model/tool change recorded",
    }


def _entry_view(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id") or "-",
        "change_type": entry.get("change_type") or "unknown",
        "target": entry.get("target") or "-",
        "summary": entry.get("summary") or "",
        "owner": entry.get("owner") or "ai-system-owner",
        "rollback": entry.get("rollback") or "",
        "evidence": entry.get("evidence") or "",
        "approval_status": entry.get("approval_status") or "recorded",
        "created_at": entry.get("created_at") or "",
    }


def _default_owner(change_type: str) -> str:
    return {
        "prompt": "prompt-owner",
        "model": "ai-ops-owner",
        "tool": "tool-data-owner",
    }.get(change_type, "ai-system-owner")


def _load_entries() -> list[dict[str, Any]]:
    if not CHANGE_LOG_PATH.exists():
        return []
    try:
        raw_entries = json.loads(CHANGE_LOG_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [entry for entry in raw_entries if isinstance(entry, dict)] if isinstance(raw_entries, list) else []


def _write_entries(entries: list[dict[str, Any]]) -> None:
    CHANGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHANGE_LOG_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
