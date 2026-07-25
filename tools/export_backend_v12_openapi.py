from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from system_app.main import create_app  # noqa: E402
from system_app.openapi_v12 import build_backend_v12_write_openapi  # noqa: E402

OUTPUT = PROJECT_ROOT / "docs" / "BACKEND_V12_WRITE_OPENAPI.json"


def main() -> None:
    contract = build_backend_v12_write_openapi(create_app())
    OUTPUT.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
