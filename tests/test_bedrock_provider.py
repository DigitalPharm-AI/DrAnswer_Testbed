from agent_app.providers.bedrock import BedrockAnthropicProvider, model_supports_temperature


def test_bedrock_provider_exposes_only_langchain_chat_model_generation():
    assert not hasattr(BedrockAnthropicProvider, "generate_json")
    assert not hasattr(BedrockAnthropicProvider, "_invoke_model")
    assert not hasattr(BedrockAnthropicProvider, "_converse_with_bearer_token")


def test_claude_sonnet_5_models_do_not_support_temperature():
    assert model_supports_temperature("global.anthropic.claude-sonnet-5") is False


def test_other_anthropic_models_keep_temperature():
    assert model_supports_temperature("global.anthropic.claude-haiku-4-5-20251001-v1:0") is True
    assert model_supports_temperature("global.anthropic.claude-sonnet-4-6") is True
