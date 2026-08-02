from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from agent_app.observability.model_calls import traced_model_ainvoke
from agent_app.providers.base import BaseLLMProvider

SEMANTIC_VERIFIER_PROMPT_VERSION = "reference-semantic-verifier-v1"


@dataclass(frozen=True)
class SemanticCandidate:
    candidate_id: str
    label: str


class SemanticMatchVerifier(Protocol):
    async def matching_candidate_ids(
        self,
        query: str,
        candidates: list[SemanticCandidate],
        *,
        domain: str,
    ) -> set[str]: ...


class SemanticVerificationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matching_candidate_ids: list[str] = Field(default_factory=list)


class LlmSemanticMatchVerifier:
    """Conservatively verifies vector candidates with one structured LLM call."""

    def __init__(
        self,
        provider: BaseLLMProvider,
        *,
        model_factory: Callable | None = None,
    ) -> None:
        self.provider = provider
        self.model_factory = model_factory

    async def matching_candidate_ids(
        self,
        query: str,
        candidates: list[SemanticCandidate],
        *,
        domain: str,
    ) -> set[str]:
        if not candidates:
            return set()
        allowed_ids = {candidate.candidate_id for candidate in candidates}
        payload = {
            "domain": domain,
            "query": query.strip(),
            "candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "label": candidate.label,
                }
                for candidate in candidates
            ],
        }
        base_model = (
            self.model_factory()
            if self.model_factory is not None
            else self.provider.semantic_verification_model()
        )
        model = base_model.with_structured_output(
            SemanticVerificationOutput
        )
        result = await traced_model_ainvoke(
            model,
            [
                SystemMessage(
                    content=(
                        "You verify semantic equivalence for Korean clinical reference lookup. "
                        "Select a candidate only when it expresses the same symptom concept as the query. "
                        "Related, co-occurring, broader, narrower, causal, or merely similar symptoms are not equivalent. "
                        "Do not decide drug causality or clinical severity. Return only the required structured result."
                    )
                ),
                HumanMessage(
                    content=json.dumps(payload, ensure_ascii=False)
                ),
            ],
            name="reference_semantic_verification",
            prompt_version_id=SEMANTIC_VERIFIER_PROMPT_VERSION,
        )
        if isinstance(result, SemanticVerificationOutput):
            selected = result.matching_candidate_ids
        elif isinstance(result, dict):
            selected = SemanticVerificationOutput.model_validate(
                result
            ).matching_candidate_ids
        else:
            selected = SemanticVerificationOutput.model_validate(
                getattr(result, "model_dump", lambda: {})()
            ).matching_candidate_ids
        return {
            candidate_id
            for candidate_id in selected
            if candidate_id in allowed_ids
        }
