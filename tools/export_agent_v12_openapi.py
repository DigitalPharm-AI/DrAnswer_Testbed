from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Contract generation must not depend on a developer's local secrets or DB files.
os.environ["AGENT_SYNC_API_TOKEN"] = "openapi-generation-agent-sync-token"
os.environ["BACKEND_API_TOKEN"] = "openapi-generation-backend-api-token"
# OpenAPI generation constructs the app but does not start its lifespan or run
# Query Tools. Clear any developer-local DB URL so schema checks never depend on
# a live Backend database.
os.environ["BACKEND_READ_DATABASE_URL"] = ""

from fastapi import FastAPI  # noqa: E402

from agent_app.openapi_v12 import build_agent_v12_chat_openapi  # noqa: E402
from agent_app.routes.chat import router as chat_router  # noqa: E402
from agent_app.routes.feedback import router as feedback_router  # noqa: E402

OUTPUT = PROJECT_ROOT / "docs" / "AI_V12_CHAT_OPENAPI.json"


def contract_app() -> FastAPI:
    # OpenAPI generation must not initialize LLM clients, workers, or live DB
    # connections. The exported v1.2 boundary is owned by this router.
    app = FastAPI(title="AI Server v1.2 contract")
    app.include_router(chat_router)
    app.include_router(feedback_router)
    return app


def rendered_contract() -> str:
    contract = build_agent_v12_chat_openapi(contract_app())
    return json.dumps(contract, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the committed OpenAPI artifact differs from runtime code.",
    )
    args = parser.parse_args()
    rendered = rendered_contract()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"OpenAPI artifact is stale: {OUTPUT}", file=sys.stderr)
            return 1
        print(f"OpenAPI artifact is current: {OUTPUT}")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
