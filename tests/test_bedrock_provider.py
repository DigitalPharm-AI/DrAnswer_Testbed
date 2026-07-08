from agent_app.bedrock_provider import model_supports_temperature


def test_claude_sonnet_5_models_do_not_support_temperature():
    assert model_supports_temperature("global.anthropic.claude-sonnet-5") is False


def test_other_anthropic_models_keep_temperature():
    assert model_supports_temperature("global.anthropic.claude-haiku-4-5-20251001-v1:0") is True
    assert model_supports_temperature("global.anthropic.claude-sonnet-4-6") is True
