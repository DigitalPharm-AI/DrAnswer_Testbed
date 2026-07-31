from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit


TOKEN_KEY = "AGENT_SYNC_API_TOKEN"
MAX_RESPONSE_BYTES = 1_048_576


def read_required_token(path: Path, key: str = TOKEN_KEY) -> str:
    matches: list[str] = []
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
            continue
        candidate_key, value = line.split("=", 1)
        if candidate_key.strip() != key:
            continue
        matches.append(value.strip().strip("\"'"))
        if len(matches) > 1:
            raise ValueError(f"{key}: duplicate assignment")
    if not matches or not matches[0]:
        raise ValueError(f"{key}: missing or empty")
    return matches[0]


def generation_readiness_url(base_url: str) -> tuple[str, int, str]:
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("generation probe requires a local HTTP base URL")
    hostname = parsed.hostname
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname.lower() == "localhost"
    if not is_loopback:
        raise ValueError("generation probe target must be loopback")
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/health/generation/ready"
    return hostname, parsed.port or 80, path


def probe_generation(
    *,
    base_url: str,
    token: str,
    timeout_seconds: float,
) -> tuple[int, str]:
    hostname, port, path = generation_readiness_url(base_url)
    connection = http.client.HTTPConnection(
        hostname,
        port,
        timeout=timeout_seconds,
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        response = connection.getresponse()
        raw_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw_body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("generation readiness response is too large")
        body = raw_body.decode("utf-8", errors="replace")
        return response.status, body
    finally:
        connection.close()


def sanitized_body(body: str, token: str) -> str:
    safe_body = body.replace(token, "[REDACTED]") if token else body
    try:
        payload = json.loads(safe_body)
    except ValueError:
        return safe_body.strip()
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Call the authenticated local generation readiness endpoint "
            "without putting its bearer token in argv."
        )
    )
    parser.add_argument("common_env_file", type=Path)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8701",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
    )
    args = parser.parse_args(argv)

    token = ""
    try:
        token = read_required_token(args.common_env_file)
        status, body = probe_generation(
            base_url=args.base_url,
            token=token,
            timeout_seconds=max(0.1, args.timeout_seconds),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        safe_error = str(exc).replace(token, "[REDACTED]") if token else str(exc)
        print(
            f"Generation readiness probe failed: {safe_error}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            "Generation readiness probe failed: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    safe_body = sanitized_body(body, token)
    if safe_body:
        print(safe_body)
    if status != 200:
        print(
            f"Generation readiness failed with HTTP {status}",
            file=sys.stderr,
        )
        return 1
    print("Generation readiness: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
