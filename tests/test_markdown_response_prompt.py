from __future__ import annotations

from agent_app.llm.prompts import (
    medication_agent_prompt,
    multiturn_chat_prompt,
    nutrition_management_agent_prompt,
    nutrition_recommendation_agent_prompt,
)


def test_user_visible_agent_prompts_share_markdown_table_rules():
    prompts = [
        multiturn_chat_prompt(),
        medication_agent_prompt(),
        nutrition_management_agent_prompt(),
        nutrition_recommendation_agent_prompt(),
    ]

    for prompt in prompts:
        assert "GitHub Flavored Markdown" in prompt
        assert "make the table the final content block" in prompt
        assert "Never emit HTML table tags" in prompt
        assert "Do not use Markdown tables for approvals" in prompt
        assert "structured server data" in prompt
