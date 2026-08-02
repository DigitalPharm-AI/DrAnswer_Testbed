from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

BEDROCK_BEARER_ENV_KEY = "AWS_BEARER_TOKEN_BEDROCK"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a real Bedrock token stream and the public error mapping "
            "for an intentionally invalid bearer token."
        ),
    )
    parser.add_argument(
        "--env-files",
        default=".env.9000,.env.agent_app.secret",
        help="Comma-separated Settings env files containing Bedrock config.",
    )
    parser.add_argument(
        "--output-root",
        default="output/playwright",
        help="Parent directory for the non-sensitive JSON result.",
    )
    return parser.parse_args()


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1_000))


def _artifact_directory(output_root: str) -> Path:
    timestamp = int(datetime.now(UTC).timestamp() * 1_000)
    suffix = uuid.uuid4().hex[:8]
    path = Path(output_root) / f"bedrock-stream-resilience-{timestamp}-{suffix}"
    path.mkdir(parents=True, exist_ok=False)
    return path


async def _verify_valid_stream(provider: Any) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage

    from agent_app.llm.messages import public_text_delta_from_ai_message
    from agent_app.providers.config import get_runtime_model_tier

    configuration = provider.configuration_readiness()
    if not configuration.ok:
        raise RuntimeError(configuration.code)

    model_tier = get_runtime_model_tier()
    model_id = provider.settings.model_id_for_tier(model_tier)
    model = provider.chat_model()
    started = perf_counter()
    first_text_ms: int | None = None
    total_chunks = 0
    public_text_chunks = 0
    public_character_count = 0

    async for chunk in model.astream(
        [HumanMessage(content="한 단어로 확인이라고 답하세요.")],
    ):
        total_chunks += 1
        delta = public_text_delta_from_ai_message(chunk)
        if not delta:
            continue
        if first_text_ms is None:
            first_text_ms = _elapsed_ms(started)
        public_text_chunks += 1
        public_character_count += len(delta)

    elapsed_ms = _elapsed_ms(started)
    if total_chunks < 1:
        raise RuntimeError("bedrock_stream_returned_no_chunks")
    if public_text_chunks < 1 or public_character_count < 1:
        raise RuntimeError("bedrock_stream_returned_no_public_text")
    return {
        "status": "passed",
        "model_tier": model_tier,
        "model_id": model_id,
        "reasoning_enabled": provider.settings.llm_reasoning_enabled,
        "reasoning_effort": provider.settings.llm_reasoning_effort,
        "total_chunks": total_chunks,
        "public_text_chunks": public_text_chunks,
        "public_character_count": public_character_count,
        "time_to_first_text_ms": first_text_ms,
        "elapsed_ms": elapsed_ms,
    }


async def _verify_invalid_bearer(provider: Any) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage
    from pydantic import SecretStr

    from agent_app.errors import public_processing_error
    from agent_app.llm.generation import agent_error

    invalid_provider = type(provider)()
    invalid_provider.settings = provider.settings.model_copy(
        update={
            "aws_bearer_token_bedrock": SecretStr(
                "intentionally-invalid-bedrock-bearer-token"
            ),
            "aws_profile": None,
            "aws_access_key_id": None,
            "aws_secret_access_key": None,
            "aws_session_token": None,
        }
    )
    model = invalid_provider.chat_model()
    started = perf_counter()
    exception: Exception | None = None
    try:
        async for _chunk in model.astream(
            [HumanMessage(content="Reply with OK.")],
        ):
            pass
    except Exception as exc:  # noqa: BLE001 - provider boundary under test
        exception = exc

    if exception is None:
        raise RuntimeError("invalid_bedrock_bearer_was_accepted")
    internal = agent_error(
        "trace_bedrock_invalid_bearer",
        "bedrock_resilience_check",
        "text",
        exception,
    )
    public = public_processing_error(internal)
    if internal.error_type != "provider_request_failed":
        raise RuntimeError("invalid_bearer_internal_error_mapping_changed")
    if public.code != "LLM_PROVIDER_REQUEST_FAILED":
        raise RuntimeError("invalid_bearer_public_error_mapping_changed")
    return {
        "status": "passed",
        "exception_type": type(exception).__name__,
        "internal_error_type": internal.error_type,
        "public_error_code": public.code,
        "retryable": public.retryable,
        "elapsed_ms": _elapsed_ms(started),
    }


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    os.environ["DA_DRUG_ENV_FILE"] = args.env_files

    from agent_app.providers.bedrock import BedrockAnthropicProvider
    from shared.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    bearer = settings.aws_bearer_token_bedrock
    if bearer is None or not bearer.get_secret_value().strip():
        raise RuntimeError("bedrock_bearer_token_not_configured")

    provider = BedrockAnthropicProvider()
    provider.settings = settings.model_copy(
        update={
            "llm_timeout_seconds": 30,
        }
    )
    original_present = BEDROCK_BEARER_ENV_KEY in os.environ
    original_value = os.environ.get(BEDROCK_BEARER_ENV_KEY)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "valid_stream": None,
        "invalid_bearer": None,
    }
    try:
        report["valid_stream"] = await _verify_valid_stream(provider)
        report["invalid_bearer"] = await _verify_invalid_bearer(provider)
        report["status"] = "passed"
    finally:
        if original_present and original_value is not None:
            os.environ[BEDROCK_BEARER_ENV_KEY] = original_value
        else:
            os.environ.pop(BEDROCK_BEARER_ENV_KEY, None)
        report["finished_at"] = datetime.now(UTC).isoformat()

    artifact_dir = _artifact_directory(args.output_root)
    artifact_path = artifact_dir / "summary.json"
    artifact_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report, artifact_path


def main() -> int:
    args = _arguments()
    try:
        report, artifact_path = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(
            json.dumps(
                {
                    "status": "failed",
                    "exception_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "valid_stream": report["valid_stream"],
                "invalid_bearer": report["invalid_bearer"],
                "artifact": str(artifact_path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
