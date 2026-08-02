from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

import boto3
import pytest
from pydantic import SecretStr

from agent_app.embeddings.cohere_bedrock import (
    BedrockCohereEmbeddingProvider,
    EmbeddingProviderError,
)
from agent_app.observability.model_calls import capture_model_calls
from shared.settings import Settings


class FakeBedrockClient:
    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        body = json.loads(kwargs["body"])
        self.requests.append({**kwargs, "body": body})
        payload = {
            "embeddings": [
                [float(index) for index in range(self.dimensions)]
                for _text in body["texts"]
            ],
            "response_type": "embeddings_floats",
        }
        return {"body": io.BytesIO(json.dumps(payload).encode("utf-8"))}


def test_embedding_settings_default_to_cohere_multilingual_tokyo():
    settings = Settings()

    assert settings.embedding_provider == "bedrock_cohere"
    assert settings.embedding_model_id == "cohere.embed-multilingual-v3"
    assert settings.embedding_aws_region == "ap-northeast-1"
    assert settings.embedding_dimensions == 1024
    assert settings.embedding_batch_size == 96


@pytest.mark.asyncio
async def test_query_uses_search_query_and_returns_1024_dimensions():
    client = FakeBedrockClient()
    provider = BedrockCohereEmbeddingProvider(client=client)

    embedding = await provider.embed_query("  속이 울렁거려요  ")

    assert len(embedding) == 1024
    assert client.requests[0]["modelId"] == "cohere.embed-multilingual-v3"
    assert client.requests[0]["body"] == {
        "texts": ["속이 울렁거려요"],
        "input_type": "search_query",
        "truncate": "NONE",
    }


@pytest.mark.asyncio
async def test_query_records_hashes_and_metrics_without_clinical_text():
    client = FakeBedrockClient()
    provider = BedrockCohereEmbeddingProvider(client=client)
    observations: list[dict] = []
    clinical_text = "속이 울렁거려요"

    with capture_model_calls(observations):
        await provider.embed_query(clinical_text)

    assert len(observations) == 1
    observation = observations[0]
    assert observation["observation_type"] == "embedding"
    assert observation["status"] == "COMPLETED"
    assert observation["model_id"] == "cohere.embed-multilingual-v3"
    assert observation["model_parameters"] == {
        "region": "ap-northeast-1",
        "input_type": "search_query",
        "text_count": 1,
        "input_characters": len(clinical_text),
        "dimensions": 1024,
    }
    assert clinical_text not in repr(observation)
    assert len(observation["input_hash"]) == 64


@pytest.mark.asyncio
async def test_documents_are_batched_and_keep_source_order():
    client = FakeBedrockClient()
    settings = Settings(embedding_batch_size=2)
    provider = BedrockCohereEmbeddingProvider(settings, client=client)

    embeddings = await provider.embed_documents(["구역", "구토", "현기증"])

    assert len(embeddings) == 3
    assert [request["body"]["texts"] for request in client.requests] == [
        ["구역", "구토"],
        ["현기증"],
    ]
    assert all(
        request["body"]["input_type"] == "search_document"
        for request in client.requests
    )


@pytest.mark.asyncio
async def test_provider_rejects_unexpected_embedding_dimensions():
    provider = BedrockCohereEmbeddingProvider(
        client=FakeBedrockClient(dimensions=8)
    )

    with pytest.raises(
        EmbeddingProviderError,
        match="cohere_embedding_response_dimensions_invalid",
    ):
        await provider.embed_query("메스꺼움")


@pytest.mark.asyncio
async def test_provider_rejects_blank_input_before_network_call():
    client = FakeBedrockClient()
    provider = BedrockCohereEmbeddingProvider(client=client)

    with pytest.raises(ValueError, match="embedding_text_must_not_be_blank"):
        await provider.embed_query("  ")

    assert client.requests == []


def test_provider_identity_does_not_contain_credentials():
    settings = Settings(
        aws_bearer_token_bedrock="secret-marker",
    )
    provider = BedrockCohereEmbeddingProvider(
        settings,
        client=SimpleNamespace(),
    )

    assert provider.identity.provider == "bedrock_cohere"
    assert provider.identity.region == "ap-northeast-1"
    assert "secret" not in repr(provider.identity)


def test_provider_builds_a_tokyo_runtime_client_and_unwraps_bearer_at_sink(
    monkeypatch,
):
    marker = "cohere-bedrock-secret-marker"
    captured: dict = {}

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session_kwargs"] = kwargs

        def client(self, service_name, *, config):
            captured["service_name"] = service_name
            captured["config"] = config
            captured["bearer"] = os.environ.get(
                "AWS_BEARER_TOKEN_BEDROCK"
            )
            return "cohere-runtime-client"

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setattr(boto3, "Session", FakeSession)
    settings = Settings(
        aws_bearer_token_bedrock=SecretStr(marker),
        embedding_aws_region="ap-northeast-1",
        embedding_timeout_seconds=7,
        embedding_max_attempts=2,
    )
    provider = BedrockCohereEmbeddingProvider(settings)

    client = provider._client()

    assert client == "cohere-runtime-client"
    assert captured["session_kwargs"]["region_name"] == "ap-northeast-1"
    assert captured["service_name"] == "bedrock-runtime"
    assert captured["config"].connect_timeout == 7
    assert captured["config"].read_timeout == 7
    assert captured["config"].retries["total_max_attempts"] == 2
    assert captured["bearer"] == marker
