from pathlib import Path

from agent_app.ae_pro_ctcae import match_pro_ctcae_symptom


def test_pro_ctcae_uses_builtin_nausea_questions_when_workbook_missing():
    result = match_pro_ctcae_symptom("메스꺼움", workbook_path=Path("data/does-not-exist-pro-ctcae.xlsx"))

    assert result.matched is True
    assert result.matched_korean_symptom_name == "메스꺼움"
    assert [question.item_code for question in result.questions] == [
        "PROCTCAE_NAUSEA_FREQUENCY",
        "PROCTCAE_NAUSEA_SEVERITY",
    ]
