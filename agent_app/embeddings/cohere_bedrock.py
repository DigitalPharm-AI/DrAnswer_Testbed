from __future__ import annotations

import asyncio
import json
import math
import os
from threading import Lock
from time import perf_counter
from typing import Any, Literal

from agent_app.embeddings.base import EmbeddingIdentity
from agent_app.observability.model_calls import (
    record_embedding_observation,
)
from shared.redaction import safe_exception_summary
from shared.settings import Settings, get_settings
from shared.time_utils import utc_now

COHERE_MAX_TEXTS_PER_REQUEST = 96
EmbeddingInputType = Literal["search_document", "search_query"]


class EmbeddingProviderError(RuntimeError):
    """Stable internal error boundary for embedding provider failures."""


class BedrockCohereEmbeddingProvider:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client_override = client
        self._runtime_client: Any | None = None
        self._client_lock = Lock()
        self._validate_configuration()

    @property
    def identity(self) -> EmbeddingIdentity:
        return EmbeddingIdentity(
            provider="bedrock_cohere",
            model_id=self.settings.embedding_model_id.strip(),
            region=self.settings.embedding_aws_region.strip(),
            dimensions=int(self.settings.embedding_dimensions),
        )

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        prepared = _prepare_texts(texts)
        if not prepared:
            return []
        return await asyncio.to_thread(
            self._embed_documents_sync,
            prepared,
        )

    async def embed_query(self, text: str) -> list[float]:
        prepared = _prepare_texts([text])
        embeddings = await asyncio.to_thread(
            self._invoke,
            prepared,
            "search_query",
        )
        return embeddings[0]

    def _embed_documents_sync(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        embeddings: list[list[float]] = []
        batch_size = min(
            int(self.settings.embedding_batch_size),
            COHERE_MAX_TEXTS_PER_REQUEST,
        )
        for offset in range(0, len(texts), batch_size):
            embeddings.extend(
                self._invoke(
                    texts[offset : offset + batch_size],
                    "search_document",
                )
            )
        return embeddings

    def _invoke(
        self,
        texts: list[str],
        input_type: EmbeddingInputType,
    ) -> list[list[float]]:
        started_at = utc_now()
        started = perf_counter()
        request_body = json.dumps(
            {
                "texts": texts,
                "input_type": input_type,
                "truncate": "NONE",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            response = self._client().invoke_model(
                modelId=self.identity.model_id,
                contentType="application/json",
                accept="application/json",
                body=request_body,
            )
            raw_body = response["body"].read()
            payload = json.loads(raw_body)
            embeddings = self._validate_response(
                payload,
                expected_count=len(texts),
            )
        except Exception as exc:
            record_embedding_observation(
                name="cohere_reference_embedding",
                provider=self.identity.provider,
                model_id=self.identity.model_id,
                region=self.identity.region,
                input_type=input_type,
                texts=texts,
                dimensions=self.identity.dimensions,
                embeddings=None,
                started_at=started_at,
                completed_at=utc_now(),
                latency_ms=round((perf_counter() - started) * 1000),
                status="ERROR",
                error_code=type(exc).__name__,
                status_message=safe_exception_summary(exc, limit=300),
            )
            if isinstance(exc, EmbeddingProviderError):
                raise
            raise EmbeddingProviderError(
                "cohere_embedding_invocation_failed"
            ) from exc
        record_embedding_observation(
            name="cohere_reference_embedding",
            provider=self.identity.provider,
            model_id=self.identity.model_id,
            region=self.identity.region,
            input_type=input_type,
            texts=texts,
            dimensions=self.identity.dimensions,
            embeddings=embeddings,
            started_at=started_at,
            completed_at=utc_now(),
            latency_ms=round((perf_counter() - started) * 1000),
            status="COMPLETED",
        )
        return embeddings

    def _validate_response(
        self,
        payload: Any,
        *,
        expected_count: int,
    ) -> list[list[float]]:
        raw_embeddings = (
            payload.get("embeddings")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(raw_embeddings, list):
            raise EmbeddingProviderError(
                "cohere_embedding_response_invalid"
            )
        if len(raw_embeddings) != expected_count:
            raise EmbeddingProviderError(
                "cohere_embedding_response_count_mismatch"
            )

        expected_dimensions = self.identity.dimensions
        embeddings: list[list[float]] = []
        for raw_embedding in raw_embeddings:
            if (
                not isinstance(raw_embedding, list)
                or len(raw_embedding) != expected_dimensions
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in raw_embedding
                )
            ):
                raise EmbeddingProviderError(
                    "cohere_embedding_response_dimensions_invalid"
                )
            embeddings.append([float(value) for value in raw_embedding])
        return embeddings

    def _client(self) -> Any:
        if self._client_override is not None:
            return self._client_override
        if self._runtime_client is not None:
            return self._runtime_client
        with self._client_lock:
            if self._runtime_client is None:
                self._runtime_client = self._build_client()
        return self._runtime_client

    def _build_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise EmbeddingProviderError(
                "cohere_embedding_dependency_missing"
            ) from exc

        bearer_token = _secret_value(
            self.settings.aws_bearer_token_bedrock
        )
        if bearer_token:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = bearer_token

        session_kwargs: dict[str, Any] = {
            "region_name": self.identity.region,
        }
        if self.settings.aws_profile:
            session_kwargs["profile_name"] = self.settings.aws_profile
        if (
            self.settings.aws_access_key_id
            and self.settings.aws_secret_access_key
        ):
            session_kwargs["aws_access_key_id"] = (
                self.settings.aws_access_key_id
            )
            session_kwargs["aws_secret_access_key"] = (
                self.settings.aws_secret_access_key
            )
        if self.settings.aws_session_token:
            session_kwargs["aws_session_token"] = (
                self.settings.aws_session_token
            )

        session = boto3.Session(**session_kwargs)
        timeout_seconds = float(self.settings.embedding_timeout_seconds)
        return session.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={
                    "mode": "standard",
                    "total_max_attempts": int(
                        self.settings.embedding_max_attempts
                    ),
                },
            ),
        )

    def _validate_configuration(self) -> None:
        if self.settings.embedding_provider.strip() != "bedrock_cohere":
            raise ValueError("unsupported_embedding_provider")
        if (
            self.settings.embedding_model_id.strip()
            != "cohere.embed-multilingual-v3"
        ):
            raise ValueError("unsupported_cohere_embedding_model")
        if not self.settings.embedding_aws_region.strip():
            raise ValueError("embedding_aws_region_required")
        if int(self.settings.embedding_dimensions) != 1024:
            raise ValueError("cohere_embedding_dimensions_must_be_1024")


def _prepare_texts(texts: list[str]) -> list[str]:
    prepared: list[str] = []
    for text in texts:
        if not isinstance(text, str):
            raise TypeError("embedding_text_must_be_string")
        value = text.strip()
        if not value:
            raise ValueError("embedding_text_must_not_be_blank")
        prepared.append(value)
    return prepared


def _secret_value(value: Any) -> str:
    if value is None:
        return ""
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        value = get_secret_value()
    return str(value).strip()
