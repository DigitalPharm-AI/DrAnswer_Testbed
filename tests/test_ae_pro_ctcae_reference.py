from pathlib import Path

import pytest

from agent_app.ae_pro_ctcae import (
    ProCtcaeReferenceUnavailable,
    match_pro_ctcae_symptom,
)
from agent_app.tools.mcp_server import AgentMcpToolServer
from shared.settings import get_settings


def test_pro_ctcae_fails_closed_when_workbook_missing():
    with pytest.raises(
        ProCtcaeReferenceUnavailable,
        match="pro_ctcae_reference_workbook_missing",
    ):
        match_pro_ctcae_symptom(
            "메스꺼움",
            workbook_path=Path(
                "data/does-not-exist-pro-ctcae.xlsx"
            ),
        )


def test_pro_ctcae_tool_returns_explicit_error_when_reference_missing(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        get_settings(),
        "pro_ctcae_workbook_path",
        tmp_path / "missing.xlsx",
    )

    result = AgentMcpToolServer._ae_pro_ctcae(
        {"symptom_text": "메스꺼움"},
        trace_id="trace-reference-missing",
    )

    assert result.status == "error"
    assert result.error == "pro_ctcae_reference_workbook_missing"
    assert result.response == {}
