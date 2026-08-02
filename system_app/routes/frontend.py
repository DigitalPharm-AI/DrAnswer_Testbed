from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

REACT_BUILD_DIR = Path(__file__).resolve().parents[1] / "static" / "react"
REACT_INDEX_PATH = REACT_BUILD_DIR / "index.html"


def create_frontend_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=FileResponse, include_in_schema=False)
    async def react_app() -> FileResponse:
        if not REACT_INDEX_PATH.is_file():
            raise HTTPException(status_code=503, detail="react_frontend_not_built")
        return FileResponse(REACT_INDEX_PATH, media_type="text/html")

    return router
