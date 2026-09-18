"""运行时 MCP 端点共用的 JSON-RPC 解析：严格 JSON、限长、拒绝重复键，结果不缓存。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request, Response

from coreman.api.errors import ApiError

NO_STORE = {"Cache-Control": "no-store"}
PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")


def response(body: dict[str, Any]) -> Response:
    return Response(
        json.dumps(body, ensure_ascii=False), media_type="application/json", headers=NO_STORE
    )


def error(rid: Any, code: int, message: str) -> Response:
    return response({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def reject_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")


async def read_body(request: Request, limit: int) -> bytes:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise ApiError(413, 413, "Request too large")
    return bytes(raw)


def parse(raw: bytes) -> tuple[Any, str, dict[str, Any]] | Response:
    """解析一条 JSON-RPC 请求，返回 (id, method, params)；不合规时直接返回错误响应。

    `_meta` 是传输层上下文，从参数里剥掉，绝不当作工具参数或身份。
    """
    try:
        body = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return error(None, -32700, "Parse error")
    if (
        not isinstance(body, dict)
        or body.get("jsonrpc") != "2.0"
        or set(body) - {"jsonrpc", "id", "method", "params"}
        or not isinstance(body.get("method"), str)
        or ("id" in body and type(body["id"]) not in (str, int, type(None)))
    ):
        return error(None, -32600, "Invalid request")
    rid, method, params = body.get("id"), body["method"], body.get("params", {})
    if not isinstance(params, dict):
        return error(rid, -32602, "Invalid params")
    if "_meta" in params:
        if not isinstance(params["_meta"], dict):
            return error(rid, -32602, "Invalid params")
        params = {key: item for key, item in params.items() if key != "_meta"}
    if "id" not in body:
        # 通知不运行工具、不改授权。
        if method != "notifications/initialized" or params:
            return error(None, -32600, "Invalid notification")
        return Response(status_code=202, headers=NO_STORE)
    return rid, method, params


def initialize(rid: Any, params: dict[str, Any], server_name: str) -> Response:
    if set(params) - {"protocolVersion", "capabilities", "clientInfo"}:
        return error(rid, -32602, "Invalid params")
    requested = params.get("protocolVersion")
    version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
    return response(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server_name, "version": "2.0"},
            },
        }
    )
