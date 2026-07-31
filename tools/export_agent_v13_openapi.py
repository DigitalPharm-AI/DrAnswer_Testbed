from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI  # noqa: E402

from agent_app.openapi_v13 import (  # noqa: E402
    build_agent_v13_async_medication_openapi,
    build_agent_v13_chat_openapi,
)
from agent_app.routes.chat import router as chat_router  # noqa: E402
from agent_app.routes.feedback import router as feedback_router  # noqa: E402
from agent_app.routes.tasks import router as task_router  # noqa: E402

OUTPUTS = {
    PROJECT_ROOT / "docs" / "AI_V13_CHAT_OPENAPI.json": (
        build_agent_v13_chat_openapi
    ),
    PROJECT_ROOT / "docs" / "AI_V13_ASYNC_MEDICATION_OPENAPI.json": (
        build_agent_v13_async_medication_openapi
    ),
}


def contract_app() -> FastAPI:
    """Create a router-only app without importing the Agent runtime."""

    app = FastAPI(
        title="닥터앤서 AI Server v1.3 계약",
        version="1.3",
    )
    app.include_router(chat_router)
    app.include_router(feedback_router)
    app.include_router(task_router)
    return app


def rendered_contracts() -> dict[Path, str]:
    app = contract_app()
    return {
        path: (
            json.dumps(builder(app), ensure_ascii=False, indent=2)
            + "\n"
        )
        for path, builder in OUTPUTS.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Fail when a committed v1.3 OpenAPI artifact differs from "
            "the router contract."
        ),
    )
    args = parser.parse_args()
    rendered = rendered_contracts()
    if args.check:
        stale = [
            path
            for path, payload in rendered.items()
            if not path.exists()
            or path.read_text(encoding="utf-8") != payload
        ]
        if stale:
            for path in stale:
                print(
                    f"OpenAPI artifact is stale: {path}",
                    file=sys.stderr,
                )
            return 1
        for path in rendered:
            print(f"OpenAPI artifact is current: {path}")
        return 0

    for path, payload in rendered.items():
        path.write_text(payload, encoding="utf-8")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
