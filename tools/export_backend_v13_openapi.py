from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI  # noqa: E402

from system_app.openapi_v13 import (  # noqa: E402
    build_backend_v13_async_callback_openapi,
    build_backend_v13_write_openapi,
)
from system_app.routes.agent_async_api import (  # noqa: E402
    create_agent_async_api_router,
)
from system_app.routes.backend_v13 import (  # noqa: E402
    create_backend_v13_router,
)
from system_app.runtime import SystemRuntime  # noqa: E402

OUTPUTS = {
    PROJECT_ROOT / "docs" / "BACKEND_V13_WRITE_OPENAPI.json": (
        build_backend_v13_write_openapi
    ),
    PROJECT_ROOT / "docs" / "BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json": (
        build_backend_v13_async_callback_openapi
    ),
}


def _runtime_not_available_during_schema_generation() -> SystemRuntime:
    raise RuntimeError("openapi_generation_runtime_not_available")


def contract_app() -> FastAPI:
    """Create a router-only app without importing the Backend runtime."""

    app = FastAPI(
        title="닥터앤서 Backend v1.3 계약",
        version="1.3",
    )
    get_runtime = _runtime_not_available_during_schema_generation
    app.include_router(
        create_agent_async_api_router(get_runtime)
    )
    app.include_router(
        create_backend_v13_router(get_runtime)
    )
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
