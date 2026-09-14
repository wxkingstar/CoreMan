"""托管 Vue 构建产物：/assets 静态文件，其余非 API 路径回退 index.html。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

RESERVED_PREFIXES = ("api", "health", "callbacks", "metrics", "docs", "openapi.json")


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    index = dist_dir / "index.html"
    resolved_dist_dir = dist_dir.resolve()
    if (dist_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str, request: Request) -> Response:
        first = full_path.split("/", 1)[0]
        if first in RESERVED_PREFIXES:
            return JSONResponse(status_code=404, content={"code": 404, "message": "接口不存在"})
        candidate = (dist_dir / full_path).resolve() if full_path else index
        if full_path and candidate.is_file() and resolved_dist_dir in candidate.parents:
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(status_code=404, content={"code": 404, "message": "管理台未构建"})
