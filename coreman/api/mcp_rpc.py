"""运行时 MCP 端点共用的 JSON-RPC 解析：严格 JSON、限长、拒绝重复键，结果不缓存。

同一个端点同时接两代协议：带 `initialize` 握手的旧版（2025-11-25 及更早），和每个请求在 `_meta`
里自带协议版本与客户端能力的无状态新版（2026-07-28）。`parse` 认出是哪一代并记在本次请求的
上下文里，`response` / `error` 据此补上新版要求的字段和 HTTP 状态码，各端点的处理代码不用分支。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request, Response

from coreman.api.errors import ApiError

NO_STORE = {"Cache-Control": "no-store"}
PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
MODERN_VERSIONS = ("2026-07-28",)
SERVER_VERSION = "2.0"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
HEADER_MISMATCH = -32020
MISSING_CAPABILITY = -32021
UNSUPPORTED_VERSION = -32022
# 握手结果一小时内不变；工具列表按人、按授权变化，不让客户端缓存。
DISCOVER_TTL_MS = 3_600_000
# 客户端发来、服务端只需收下的通知。请求都是同步的一问一答，取消通知不需要动作。
NOTIFICATIONS = ("notifications/initialized", "notifications/cancelled")
_BASE64_PREFIX, _BASE64_SUFFIX = "=?base64?", "?="


@dataclass
class _Call:
    """本次请求的协议上下文。"""

    modern: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    server: str = ""


_call: ContextVar[_Call | None] = ContextVar("mcp_call", default=None)


def _current() -> _Call:
    return _call.get() or _Call()


def modern() -> bool:
    """本次请求是不是无状态新版协议。"""
    return _current().modern


def client_extension(name: str) -> bool:
    """本次请求的客户端有没有声明支持这个扩展。"""
    extensions = _current().capabilities.get("extensions")
    return isinstance(extensions, dict) and isinstance(extensions.get(name), dict)


def response(body: dict[str, Any], status: int = 200) -> Response:
    call = _current()
    result = body.get("result")
    if call.modern and isinstance(result, dict):
        result = {"resultType": "complete", **result}
        if "tools" in result:
            result = {"ttlMs": 0, "cacheScope": "private", **result}
        if call.server:
            meta = {
                META_SERVER: {"name": call.server, "version": SERVER_VERSION},
                **(result.get("_meta") or {}),
            }
            result = {**result, "_meta": meta}
        body = {**body, "result": result}
    return Response(
        json.dumps(body, ensure_ascii=False),
        status_code=status,
        media_type="application/json",
        headers=NO_STORE,
    )


def error(
    rid: Any, code: int, message: str, *, data: Any = None, status: int | None = None
) -> Response:
    detail: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        detail["data"] = data
    if status is None:
        if code in (HEADER_MISMATCH, MISSING_CAPABILITY, UNSUPPORTED_VERSION):
            status = 400
        elif code == -32601 and modern():
            status = 404
        else:
            status = 200
    return Response(
        json.dumps({"jsonrpc": "2.0", "id": rid, "error": detail}, ensure_ascii=False),
        status_code=status,
        media_type="application/json",
        headers=NO_STORE,
    )


def missing_capability(rid: Any, extension: str) -> Response:
    return error(
        rid,
        MISSING_CAPABILITY,
        "Missing required client capability",
        data={"requiredCapabilities": {"extensions": {extension: {}}}},
    )


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


def _header_value(value: str) -> str | None:
    """`Mcp-Name` 的原值；放不进 ASCII 头的名字按规范包成 `=?base64?…?=`。"""
    if not (value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX)):
        return value
    try:
        return base64.b64decode(
            value[len(_BASE64_PREFIX) : -len(_BASE64_SUFFIX)], validate=True
        ).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None


def _modern_checks(
    rid: Any, method: str, params: dict[str, Any], meta: dict[str, Any], headers: Mapping[str, str]
) -> Response | None:
    """新版请求的自述字段和镜像请求头；不合规时返回错误响应。"""
    version = meta.get(META_VERSION)
    if not isinstance(version, str):
        return error(rid, -32602, "Invalid params: missing protocol version", status=400)
    if headers.get("mcp-protocol-version") != version:
        return error(
            rid,
            HEADER_MISMATCH,
            "Header mismatch: MCP-Protocol-Version does not match the request body",
        )
    if version not in MODERN_VERSIONS:
        return error(
            rid,
            UNSUPPORTED_VERSION,
            "Unsupported protocol version",
            data={
                "supported": [*MODERN_VERSIONS, *reversed(PROTOCOL_VERSIONS)],
                "requested": version,
            },
        )
    capabilities = meta.get(META_CAPABILITIES)
    if not isinstance(capabilities, dict):
        return error(rid, -32602, "Invalid params: missing client capabilities", status=400)
    if headers.get("mcp-method") != method:
        return error(
            rid, HEADER_MISMATCH, "Header mismatch: Mcp-Method does not match the request body"
        )
    named = (
        params.get("name")
        if method == "tools/call"
        else params.get("taskId")
        if method.startswith("tasks/")
        else None
    )
    if isinstance(named, str) and _header_value(headers.get("mcp-name", "")) != named:
        return error(
            rid, HEADER_MISMATCH, "Header mismatch: Mcp-Name does not match the request body"
        )
    _call.set(_Call(modern=True, capabilities=capabilities))
    # 握手和保活只属于旧版。
    if method in ("initialize", "ping"):
        return error(rid, -32601, "Method not found")
    return None


def parse(raw: bytes, headers: Mapping[str, str]) -> tuple[Any, str, dict[str, Any]] | Response:
    """解析一条 JSON-RPC 请求，返回 (id, method, params)；不合规时直接返回错误响应。

    `_meta` 是传输层上下文，从参数里剥掉，绝不当作工具参数或身份。
    """
    _call.set(_Call())
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
    meta: dict[str, Any] = {}
    if "_meta" in params:
        if not isinstance(params["_meta"], dict):
            return error(rid, -32602, "Invalid params")
        meta = params["_meta"]
        params = {key: item for key, item in params.items() if key != "_meta"}
    if "id" not in body:
        # 通知不运行工具、不改授权。
        if method not in NOTIFICATIONS or (method == "notifications/initialized" and params):
            return error(None, -32600, "Invalid notification")
        return Response(status_code=202, headers=NO_STORE)
    if META_VERSION in meta or headers.get("mcp-protocol-version") in MODERN_VERSIONS:
        failed = _modern_checks(rid, method, params, meta, headers)
        if failed is not None:
            return failed
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
                "serverInfo": {"name": server_name, "version": SERVER_VERSION},
            },
        }
    )


def hello(
    rid: Any,
    method: str,
    params: dict[str, Any],
    server_name: str,
    *,
    extensions: dict[str, Any] | None = None,
) -> Response | None:
    """两代协议各自的开场：旧版 `initialize`，新版 `server/discover`。别的方法返回 None。

    每个端点解析完请求先调它：服务名也在这里记下，新版的每个结果都带上。
    """
    call = _current()
    call.server = server_name
    if method == "initialize":
        return initialize(rid, params, server_name)
    if method != "server/discover" or not call.modern:
        return None
    if params:
        return error(rid, -32602, "Invalid params")
    capabilities: dict[str, Any] = {"tools": {}}
    if extensions:
        capabilities["extensions"] = extensions
    return response(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "supportedVersions": list(MODERN_VERSIONS),
                "capabilities": capabilities,
                "ttlMs": DISCOVER_TTL_MS,
                "cacheScope": "public",
            },
        }
    )
