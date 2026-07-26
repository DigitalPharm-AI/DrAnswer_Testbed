from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Contract generation must stay deterministic after runtime authentication became
# fail-closed. These non-production values only allow module construction; they
# are never written into the OpenAPI document.
os.environ["AGENT_SYNC_API_TOKEN"] = "openapi-generation-agent-sync-token"
os.environ["BACKEND_API_TOKEN"] = "openapi-generation-backend-api-token"
os.environ["BACKEND_READ_DATABASE_URL"] = (
    "sqlite:///file:./runtime/system/system.db?mode=ro&uri=true"
)

from system_app.main import create_app  # noqa: E402
from system_app.openapi_v12 import build_backend_v12_write_openapi  # noqa: E402

OUTPUT = PROJECT_ROOT / "docs" / "BACKEND_V12_WRITE_OPENAPI.json"


def rendered_contract() -> str:
    contract = build_backend_v12_write_openapi(create_app())
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
