from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit


REQUIRED_VALUES = (
    "APP_ENV",
    "LLM_PROVIDER",
    "BEDROCK_AUTH_MODE",
    "AGENT_DATABASE_URL",
    "AGENT_MIGRATION_DATABASE_URL",
    "BACKEND_READ_DATABASE_URL",
    "SYSTEM_BASE_URL",
    "INTERNAL_API_TOKEN",
    "BACKEND_API_TOKEN",
    "AGENT_SYNC_API_TOKEN",
    "AGENT_FEEDBACK_ENCRYPTION_KEY",
    "AGENT_FEEDBACK_ENCRYPTION_KEY_ID",
    "PRO_CTCAE_WORKBOOK_PATH",
)
POSTGRES_URLS = (
    "AGENT_DATABASE_URL",
    "AGENT_MIGRATION_DATABASE_URL",
    "BACKEND_READ_DATABASE_URL",
)
TOKEN_KEYS = (
    "INTERNAL_API_TOKEN",
    "BACKEND_API_TOKEN",
    "AGENT_SYNC_API_TOKEN",
)
PLACEHOLDER_MARKERS = ("CHANGE_ME", "<", ">")
BEDROCK_AUTH_MODES = {"bearer_token", "iam_role"}
BEDROCK_BEARER_KEY = "AWS_BEARER_TOKEN_BEDROCK"
COMMON_FORBIDDEN_AWS_CREDENTIAL_KEYS = (
    BEDROCK_BEARER_KEY,
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
BEDROCK_FILE_ALLOWED_KEYS = {BEDROCK_BEARER_KEY}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise ValueError(f"line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"line {line_number}: invalid key")
        values[key] = value.strip().strip("\"'")
    return values


def validate(
    values: dict[str, str],
    bedrock_values: dict[str, str] | None = None,
) -> list[str]:
    bedrock_values = bedrock_values or {}
    errors: list[str] = []
    for key in REQUIRED_VALUES:
        value = values.get(key, "").strip()
        if not value:
            errors.append(f"{key}: missing or empty")
        elif any(marker in value for marker in PLACEHOLDER_MARKERS):
            errors.append(f"{key}: placeholder remains")

    for key in COMMON_FORBIDDEN_AWS_CREDENTIAL_KEYS:
        if key in values:
            errors.append(
                f"{key}: forbidden in common agent.env; "
                "use bedrock.env and the stdin installer"
            )
    for key in bedrock_values:
        if key not in BEDROCK_FILE_ALLOWED_KEYS:
            errors.append(f"{key}: not allowed in bedrock.env")

    auth_mode = values.get("BEDROCK_AUTH_MODE", "").strip().lower()
    bearer_token = bedrock_values.get(BEDROCK_BEARER_KEY, "").strip()
    if auth_mode not in BEDROCK_AUTH_MODES:
        errors.append(
            "BEDROCK_AUTH_MODE: must be bearer_token or iam_role"
        )
    elif auth_mode == "bearer_token":
        if not bearer_token:
            errors.append(
                f"{BEDROCK_BEARER_KEY}: missing from bedrock.env"
            )
        elif any(
            marker in bearer_token for marker in PLACEHOLDER_MARKERS
        ):
            errors.append(
                f"{BEDROCK_BEARER_KEY}: placeholder remains in bedrock.env"
            )
    elif bearer_token:
        errors.append(
            f"{BEDROCK_BEARER_KEY}: must be empty when "
            "BEDROCK_AUTH_MODE=iam_role"
        )

    for key in POSTGRES_URLS:
        value = values.get(key, "")
        try:
            parsed = urlsplit(value.replace("postgresql+psycopg://", "postgresql://", 1))
        except ValueError:
            parsed = None
        if (
            not value.startswith("postgresql+psycopg://")
            or parsed is None
            or not parsed.hostname
            or not parsed.path.strip("/")
        ):
            errors.append(f"{key}: valid postgresql+psycopg URL required")

    system_base_url = values.get("SYSTEM_BASE_URL", "")
    try:
        parsed_system_url = urlsplit(system_base_url)
    except ValueError:
        parsed_system_url = None
    if (
        parsed_system_url is None
        or parsed_system_url.scheme not in {"http", "https"}
        or not parsed_system_url.hostname
    ):
        errors.append("SYSTEM_BASE_URL: valid http(s) URL required")

    tokens = [values.get(key, "").strip() for key in TOKEN_KEYS]
    for key, token in zip(TOKEN_KEYS, tokens, strict=True):
        if token and len(token) < 32:
            errors.append(f"{key}: use at least 32 random characters")
    if len({token for token in tokens if token}) != len([token for token in tokens if token]):
        errors.append("service tokens must all be different")

    encoded_key = values.get("AGENT_FEEDBACK_ENCRYPTION_KEY", "").strip()
    if encoded_key and not any(
        marker in encoded_key for marker in PLACEHOLDER_MARKERS
    ):
        try:
            decoded_key = base64.b64decode(
                encoded_key + ("=" * (-len(encoded_key) % 4)),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, binascii.Error):
            errors.append(
                "AGENT_FEEDBACK_ENCRYPTION_KEY: invalid URL-safe base64"
            )
        else:
            if len(decoded_key) != 32:
                errors.append(
                    "AGENT_FEEDBACK_ENCRYPTION_KEY: must decode to 32 bytes"
                )

    if values.get("AGENT_STARTUP_MIGRATIONS_ENABLED", "").lower() != "false":
        errors.append("AGENT_STARTUP_MIGRATIONS_ENABLED: must be false")
    if values.get("AGENT_EMBEDDED_WORKER_ENABLED", "").lower() != "false":
        errors.append("AGENT_EMBEDDED_WORKER_ENABLED: must be false")

    langfuse_enabled = values.get(
        "LANGFUSE_EXPORT_ENABLED",
        "",
    ).strip().lower() in {"true", "1", "yes"}
    if langfuse_enabled:
        for key in (
            "LANGFUSE_BASE_URL",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
        ):
            value = values.get(key, "").strip()
            if not value:
                errors.append(f"{key}: missing or empty")
            elif any(
                marker in value for marker in PLACEHOLDER_MARKERS
            ):
                errors.append(f"{key}: placeholder remains")
        try:
            parsed_langfuse_url = urlsplit(
                values.get("LANGFUSE_BASE_URL", "")
            )
        except ValueError:
            parsed_langfuse_url = None
        if (
            parsed_langfuse_url is None
            or parsed_langfuse_url.scheme != "https"
            or not parsed_langfuse_url.hostname
        ):
            errors.append(
                "LANGFUSE_BASE_URL: valid private HTTPS URL required"
            )
        if values.get("LANGFUSE_VERIFY_TLS", "").lower() != "true":
            errors.append("LANGFUSE_VERIFY_TLS: must be true")
        if values.get("LANGFUSE_RETENTION_DAYS", "") != "1095":
            errors.append("LANGFUSE_RETENTION_DAYS: must be 1095")
        for key in (
            "AGENT_COST_INPUT_USD_PER_1M_TOKENS",
            "AGENT_COST_OUTPUT_USD_PER_1M_TOKENS",
        ):
            try:
                configured_price = float(values.get(key, ""))
            except ValueError:
                configured_price = 0.0
            if configured_price <= 0:
                errors.append(f"{key}: positive approved price required")

    return errors


def validate_bedrock_file_metadata(path: Path) -> list[str]:
    if os.name != "posix" or not path.exists():
        return []
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    errors: list[str] = []
    if metadata.st_uid != 0:
        errors.append("bedrock.env: owner must be root")
    if mode != 0o600:
        errors.append("bedrock.env: mode must be 0600")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the EC2 agent environment without printing secrets."
    )
    parser.add_argument(
        "common_env_file",
        type=Path,
        help="common Agent environment without AWS credentials",
    )
    parser.add_argument(
        "bedrock_env_file",
        type=Path,
        help="API/worker-only Bedrock credential environment",
    )
    args = parser.parse_args()

    if not args.common_env_file.is_file():
        print(
            f"ERROR: common env file not found: {args.common_env_file}",
            file=sys.stderr,
        )
        return 2

    try:
        values = read_env(args.common_env_file)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: cannot parse common env file: {exc}", file=sys.stderr)
        return 2
    if args.bedrock_env_file.exists():
        if not args.bedrock_env_file.is_file():
            print(
                f"ERROR: Bedrock env path is not a file: "
                f"{args.bedrock_env_file}",
                file=sys.stderr,
            )
            return 2
        try:
            bedrock_values = read_env(args.bedrock_env_file)
        except (OSError, UnicodeError, ValueError) as exc:
            print(
                f"ERROR: cannot parse Bedrock env file: {exc}",
                file=sys.stderr,
            )
            return 2
    else:
        bedrock_values = {}

    errors = validate(values, bedrock_values)
    errors.extend(validate_bedrock_file_metadata(args.bedrock_env_file))
    if errors:
        print("Agent environment is not ready:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Agent environment validation: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
