from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from coreman import __version__

router = APIRouter()


@router.get("/health", include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    try:
        async with asyncio.timeout(2):
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return JSONResponse(
            status_code=503, content={"status": "degraded", "version": __version__, "db": "error"}
        )
    return JSONResponse(content={"status": "ok", "version": __version__, "db": "ok"})
