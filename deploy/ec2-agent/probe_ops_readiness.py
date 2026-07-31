from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from probe_generation import MAX_RESPONSE_BYTES, read_required_token


TOKEN_KEY = "INTERNAL_API_TOKEN"


def ops_readiness_url(base_url: str) -> tuple[str, int, str]:
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ops probe requires a local HTTP base URL")
    hostname = parsed.hostname
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname.lower() == "localhost"
    if not is_loopback:
        raise ValueError("ops probe target must be loopback")
    base_path = parsed.path.rstrip("/")
    return hostname, parsed.port or 80, f"{base_path}/agent/ops/readiness"


def probe_ops(
    *,
    base_url: str,
    token: str,
    timeout_seconds: float,
) -> tuple[int, str]:
    hostname, port, path = ops_readiness_url(base_url)
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
                "X-Internal-API-Token": token,
            },
        )
        response = connection.getresponse()
        raw_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw_body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("ops readiness response is too large")
        return response.status, raw_body.decode("utf-8", errors="replace")
    finally:
        connection.close()


def _safe_payload(body: str, token: str) -> dict:
    safe_body = body.replace(token, "[REDACTED]") if token else body
    payload = json.loads(safe_body)
    if not isinstance(payload, dict):
        raise ValueError("ops readiness response must be an object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Wait for an authenticated, non-critical Agent ops readiness."
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
        default=60.0,
    )
    args = parser.parse_args(argv)

    token = ""
    deadline = time.monotonic() + max(0.1, args.timeout_seconds)
    last_summary = "unavailable"
    try:
        token = read_required_token(args.common_env_file, TOKEN_KEY)
        while True:
            status, body = probe_ops(
                base_url=args.base_url,
                token=token,
                timeout_seconds=min(10.0, max(0.1, args.timeout_seconds)),
            )
            payload = _safe_payload(body, token)
            ops_status = str(payload.get("status") or "")
            metrics = payload.get("metrics")
            running_workers = (
                int(metrics.get("running_worker_count") or 0)
                if isinstance(metrics, dict)
                else 0
            )
            last_summary = (
                f"http={status},status={ops_status},"
                f"running_workers={running_workers}"
            )
            # Warning-only historical state (for example a stale worker row)
            # must not make a healthy new release impossible to activate.
            # Critical readiness remains a hard failure.
            acceptable_status = ops_status in {"ok", "degraded"}
            if (
                status == 200
                and acceptable_status
                and running_workers >= 1
            ):
                print(
                    "Agent ops readiness: "
                    f"{ops_status}, running_workers={running_workers}"
                )
                return 0
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
    except Exception as exc:
        safe_error = (
            str(exc).replace(token, "[REDACTED]") if token else str(exc)
        )
        print(
            f"Agent ops readiness probe failed: {safe_error}",
            file=sys.stderr,
        )
        return 1
    print(
        f"Agent ops readiness did not pass: {last_summary}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
