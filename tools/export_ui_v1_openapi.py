from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI  # noqa: E402

from system_app.routes.ui_api import create_ui_api_router  # noqa: E402
from system_app.routes.ui_feedback import create_ui_feedback_router  # noqa: E402

OUTPUT = PROJECT_ROOT / "docs" / "UI_V1_OPENAPI.json"


def contract_app() -> FastAPI:
    app = FastAPI(
        title="React-Backend UI BFF v1 contract",
        version="1.0.0",
    )
    runtime = SimpleNamespace()

    def get_runtime() -> SimpleNamespace:
        return runtime

    app.include_router(create_ui_api_router(get_runtime))
    app.include_router(create_ui_feedback_router(get_runtime))
    return app


def rendered_contract() -> str:
    return json.dumps(
        contract_app().openapi(),
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the committed UI OpenAPI artifact differs from runtime code.",
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
