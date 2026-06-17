from __future__ import annotations

from fastapi.responses import Response


def hx_refresh() -> Response:
    return Response(status_code=204, headers={"HX-Refresh": "true"})
