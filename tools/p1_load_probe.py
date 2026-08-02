from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.readiness_budget import (  # noqa: E402
    LoadBudget,
    evaluate_load_budget,
    summarize_probe_samples,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the P1 pilot load probe for DA_drug services.")
    parser.add_argument("--system-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--agent-base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--internal-api-token", default="")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--p95-health-ms", type=int, default=1000)
    parser.add_argument("--p95-async-accept-ms", type=int, default=2000)
    parser.add_argument("--p95-ui-status-ms", type=int, default=2000)
    return parser


async def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    headers = {"X-Internal-Api-Token": args.internal_api_token} if args.internal_api_token else {}
    workflows = [
        ("system_health", "GET", f"{args.system_base_url.rstrip('/')}/health", None, {}),
        ("agent_health", "GET", f"{args.agent_base_url.rstrip('/')}/health", None, {}),
        ("agent_readiness", "GET", f"{args.agent_base_url.rstrip('/')}/agent/ops/readiness", None, headers),
        ("ui_status", "GET", f"{args.system_base_url.rstrip('/')}/api/ui/v1/status", None, {}),
    ]

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        tasks = []
        for index in range(max(1, args.iterations)):
            workflow = workflows[index % len(workflows)]
            tasks.append(_timed_request(client, semaphore, workflow))
        samples = await asyncio.gather(*tasks)

    summary = summarize_probe_samples(samples)
    budget = LoadBudget(
        concurrency_target=args.concurrency,
        max_error_rate=args.max_error_rate,
        p95_health_ms=args.p95_health_ms,
        p95_async_accept_ms=args.p95_async_accept_ms,
        p95_ui_status_ms=args.p95_ui_status_ms,
    )
    budget_result = evaluate_load_budget(summary, budget)
    return {
        "status": budget_result["status"],
        "summary": summary,
        "budget": {
            "concurrency_target": budget.concurrency_target,
            "max_error_rate": budget.max_error_rate,
            "p95_health_ms": budget.p95_health_ms,
            "p95_async_accept_ms": budget.p95_async_accept_ms,
            "p95_ui_status_ms": budget.p95_ui_status_ms,
        },
        "alerts": budget_result["alerts"],
    }


async def _timed_request(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    workflow: tuple[str, str, str, dict[str, Any] | None, dict[str, str]],
) -> dict[str, Any]:
    workflow_name, method, url, payload, headers = workflow
    started = perf_counter()
    status_code = 0
    ok = False
    error = ""
    async with semaphore:
        try:
            response = await client.request(method, url, json=payload, headers=headers)
            status_code = response.status_code
            ok = response.is_success
        except httpx.HTTPError as exc:
            error = exc.__class__.__name__
    elapsed_ms = round((perf_counter() - started) * 1000, 2)
    sample = {
        "workflow": workflow_name,
        "method": method,
        "status_code": status_code,
        "ok": ok,
        "elapsed_ms": elapsed_ms,
    }
    if error:
        sample["error"] = error
    return sample


def main() -> None:
    args = build_parser().parse_args()
    result = asyncio.run(run_probe(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
