from __future__ import annotations

import pytest

from agent_app.embeddings import semantic_verifier as verifier_module
from agent_app.embeddings.semantic_verifier import (
    LlmSemanticMatchVerifier,
    SemanticCandidate,
)
from agent_app.providers.base import BaseLLMProvider


class FakeStructuredModel:
    def __init__(self) -> None:
        self.schema = None

    def with_structured_output(self, schema):
        self.schema = schema
        return self


class FakeProvider(BaseLLMProvider):
    def __init__(self) -> None:
        self.model = FakeStructuredModel()
        self.semantic_model_calls = 0

    def chat_model(self):
        raise AssertionError("the regular response model must not be used")

    def semantic_verification_model(self):
        self.semantic_model_calls += 1
        return self.model


@pytest.mark.asyncio
async def test_semantic_verifier_uses_one_structured_call_and_filters_unknown_ids(
    monkeypatch,
):
    captured: dict = {}

    async def fake_traced_model_ainvoke(model, messages, **kwargs):
        captured["model"] = model
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {
            "matching_candidate_ids": ["candidate-2", "not-allowed"]
        }

    monkeypatch.setattr(
        verifier_module,
        "traced_model_ainvoke",
        fake_traced_model_ainvoke,
    )
    provider = FakeProvider()
    verifier = LlmSemanticMatchVerifier(provider)

    selected = await verifier.matching_candidate_ids(
        "속이 울렁거려요",
        [
            SemanticCandidate("candidate-1", "구토"),
            SemanticCandidate("candidate-2", "메스꺼움"),
        ],
        domain="pro_ctcae_symptom_alias",
    )

    assert selected == {"candidate-2"}
    assert provider.semantic_model_calls == 1
    assert provider.model.schema is not None
    assert captured["model"] is provider.model
    assert captured["kwargs"] == {
        "name": "reference_semantic_verification",
        "prompt_version_id": "reference-semantic-verifier-v1",
    }
    assert "not equivalent" in captured["messages"][0].content
    assert "candidate-2" in captured["messages"][1].content


@pytest.mark.asyncio
async def test_semantic_verifier_skips_model_when_candidates_are_empty():
    provider = FakeProvider()
    verifier = LlmSemanticMatchVerifier(provider)

    selected = await verifier.matching_candidate_ids(
        "메스꺼움",
        [],
        domain="mfds_adverse_reaction",
    )

    assert selected == set()
    assert provider.semantic_model_calls == 0
