"""Proxied business system calls (`systems_call`) for systems with `token_delivery = proxy`.

The platform calls the system on the member's behalf, so the token never reaches the runtime.
Only operations in the catalog that the member can see are callable, arguments are validated
against the operation's schemas, `write` needs the AI employee's grant to allow writes, and
`destructive`/`financial` are never proxied. The response is bounded and every call that reaches
a policy decision is recorded without arguments, bodies or the token.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from coreman.core.auth.token_providers import TokenProviderError
from coreman.core.db.models import BusinessSystem, SystemCall
from coreman.core.logging import get_logger
from coreman.core.systems_catalog import context, fetch
from coreman.core.systems_catalog.compiler import same_origin
from coreman.core.systems_catalog.context import Context
from coreman.core.systems_catalog.validate import Validator, coerce

log = get_logger(__name__)

CALL_TIMEOUT = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RESULT_MAX = 100_000
ERROR_MAX = 4000
LIST_STEPS = (200, 100, 50, 20, 10, 5, 2, 1)
NEVER_PROXIED = ("destructive", "financial")
DATA_TRUST = {"content_trust": "system_returned_data"}
_TEXT_TYPES = ("json", "text/", "xml", "yaml", "javascript", "x-www-form-urlencoded")

Scalar = str | int | float | bool


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    system: str = Field(min_length=1, max_length=50, description="System key, e.g. stock")
    operation_id: str = Field(min_length=1, max_length=100)
    path_params: dict[str, Scalar] | None = Field(
        default=None, description="Values for {placeholders} in the path"
    )
    query: dict[str, Scalar | list[Scalar]] | None = Field(
        default=None, description="Query parameters"
    )
    body: Any = Field(default=None, description="JSON request body")


DESCRIPTION = (
    "Call an operation of a system marked as platform-proxied, as the current member. Check it "
    "with systems_describe first. Reads run directly; writes need the AI employee's write "
    "permission for the system; destructive and financial operations are never proxied."
)


def _text(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_request(
    op: dict[str, Any], defs: dict[str, Any], args: Call
) -> tuple[str, list[tuple[str, str]], Any, list[str]]:
    """(path, query pairs, body, problems): arguments checked against the operation's schemas."""
    spec = op["validate"]
    validator = Validator(defs)
    problems: list[str] = []
    path = op["path"]
    given_path = dict(args.path_params or {})
    given_query = dict(args.query or {})
    pairs: list[tuple[str, str]] = []
    for param in spec["params"]:
        name, where = param["name"], param["in"]
        if where == "path":
            if name not in given_path:
                problems.append(f"path_params.{name}: required")
                continue
            value = coerce(given_path.pop(name), param["schema"], validator)
            problems += validator.errors(value, param["schema"], f"path_params.{name}")
            text = _text(value)
            if not text:
                problems.append(f"path_params.{name}: must not be empty")
            # Encoded as one segment: a value can never add path segments or a query.
            path = path.replace("{" + name + "}", quote(text, safe=""))
        elif where == "query":
            if name not in given_query:
                if param["required"]:
                    problems.append(f"query.{name}: required")
                continue
            value = coerce(given_query.pop(name), param["schema"], validator)
            problems += validator.errors(value, param["schema"], f"query.{name}")
            values = value if isinstance(value, list) else [value]
            pairs += [(name, _text(v)) for v in values]
        elif param["required"]:
            problems.append(f"{name}: {where} parameters are not supported by systems_call")
    problems += [f"path_params.{name}: unknown parameter" for name in given_path]
    problems += [f"query.{name}: unknown parameter" for name in given_query]
    body_spec = spec["body"]
    if body_spec is None:
        if args.body is not None:
            problems.append("body: this operation takes no request body")
    elif not body_spec["json"]:
        problems.append("body: only JSON request bodies are supported by systems_call")
    elif args.body is None:
        if body_spec["required"]:
            problems.append("body: required")
    else:
        problems += validator.errors(args.body, body_spec["schema"], "body")
    if "{" in path:
        problems.append("path: undeclared path parameter in the catalog")
    return path, pairs, args.body, problems


def _cut(value: Any, keep: int) -> tuple[Any, int]:
    """Lists cut to `keep` items (and long strings shortened); returns the omitted item count."""
    if isinstance(value, list):
        omitted = max(0, len(value) - keep)
        items, inner = [], 0
        for item in value[:keep]:
            cut, n = _cut(item, keep)
            items.append(cut)
            inner += n
        return items, omitted + inner
    if isinstance(value, dict):
        out, total = {}, 0
        for key, item in value.items():
            out[key], n = _cut(item, keep)
            total += n
        return out, total
    if isinstance(value, str) and keep <= 20 and len(value) > 1000:
        return value[:1000] + "…", 0
    return value, 0


def fit_json(data: Any, limit: int) -> tuple[Any, int | None]:
    """Data within `limit` characters: (data, None) when whole, (trimmed, omitted) otherwise."""
    if len(json.dumps(data, ensure_ascii=False)) <= limit:
        return data, None
    for keep in LIST_STEPS:
        trimmed, omitted = _cut(data, keep)
        if len(json.dumps(trimmed, ensure_ascii=False)) <= limit:
            return trimmed, omitted
    text = json.dumps(data, ensure_ascii=False)[:limit]
    return text, -1


def shape(response: httpx.Response, raw: bytes, cut_off: bool, secret: str) -> dict[str, Any]:
    status = response.status_code
    out: dict[str, Any] = {"status": status, "ok": 200 <= status < 300}
    if 300 <= status < 400:
        out["message"] = "业务系统返回了跳转，平台不跟随跳转。"
        return out
    content_type = response.headers.get("content-type", "")
    disposition = response.headers.get("content-disposition", "")
    base = content_type.split(";", 1)[0].strip().lower()
    textual = not base or any(marker in base for marker in _TEXT_TYPES)
    if "attachment" in disposition.lower() or not textual:
        # Files: metadata only, never the bytes.
        out.update(content_type=base or None, bytes=len(raw), file=True)
        if cut_off:
            out["bytes_at_least"] = True
        return out
    text = raw.decode("utf-8", errors="replace")
    if secret:
        # A system that echoes request headers must not hand the token to the model.
        text = text.replace(secret, "[REDACTED]")
    limit = RESULT_MAX if out["ok"] else ERROR_MAX
    data: Any = None
    if "json" in base and not cut_off:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if data is not None:
        out["data"], omitted = fit_json(data, limit)
        if omitted is not None:
            out["truncated"] = True
            out["hint"] = (
                f"响应超过 {limit} 字符，已截断"
                + (f"（省略 {omitted} 个列表元素）" if omitted > 0 else "")
                + "。请用分页、筛选或时间范围参数缩小结果后再查。"
            )
    else:
        out["text"] = text[:limit]
        if cut_off or len(text) > limit:
            out["truncated"] = True
            out["hint"] = "响应过大，已截断。请用分页、筛选或时间范围参数缩小结果后再查。"
    return out


async def _record(
    ctx: Context,
    system: BusinessSystem,
    operation_id: str,
    op: dict[str, Any],
    outcome: str,
    *,
    status: int | None = None,
    duration_ms: int | None = None,
    token_id: str | None = None,
) -> None:
    ctx.session.add(
        SystemCall(
            task_id=ctx.operator.task_id,
            bot_id=ctx.operator.bot_id,
            user_id=ctx.operator.user.id,
            system_key=system.key,
            operation_id=operation_id,
            method=op["method"],
            risk=op["risk"],
            outcome=outcome,
            status_code=status,
            duration_ms=duration_ms,
            token_id=token_id,
        )
    )
    await ctx.session.commit()
    log.info(
        "system_call",
        task_id=ctx.operator.task_id,
        system=system.key,
        operation=operation_id,
        risk=op["risk"],
        outcome=outcome,
        status=status,
        duration_ms=duration_ms,
        token_id=token_id,
    )


def policy_error(ctx: Context, system: BusinessSystem, op: dict[str, Any]) -> str | None:
    """Why this operation may not be proxied for this AI employee, or None."""
    if op["risk"] in NEVER_PROXIED:
        return "risk_not_allowed"
    if op["risk"] == "write" and system.key not in ctx.writable:
        return "write_not_allowed"
    return None


DENIALS = {
    "risk_not_allowed": "不可撤销或涉及资金的操作（destructive、financial）不通过平台代理执行。"
    "请告诉用户需要本人在业务系统里操作。",
    "write_not_allowed": "这个 AI 员工没有该系统的写入权限。需要管理员在 AI 员工的「系统权限」里"
    "为这个系统开启「允许写入」；在此之前不要尝试其他写入方式。",
}


async def call(ctx: Context, args: Call) -> dict[str, Any]:
    system = ctx.system(args.system)
    if system is None:
        return context.no_system(args.system)
    if system.token_delivery != "proxy":
        return {
            "error": "not_proxied",
            "system": system.key,
            "message": "这个系统不经平台代理：按 systems_describe 给出的 curl 模板，"
            "用本轮的业务系统令牌变量调用。",
        }
    loaded = await context.catalog(ctx, system)
    if loaded is None:
        return context.unavailable(system)
    op = loaded.compiled["operations"].get(args.operation_id)
    codes = await context.held(ctx, system, loaded) if op is not None else None
    if op is None or not context.visible(op, codes):
        return context.operation_not_found(system, args.operation_id)
    if "validate" not in op:
        return context.unavailable(system)
    denied = policy_error(ctx, system, op)
    if denied is not None:
        await _record(ctx, system, args.operation_id, op, "denied")
        return {
            "error": denied,
            "system": system.key,
            "operation_id": args.operation_id,
            "risk": op["risk"],
            "message": DENIALS[denied],
        }
    path, pairs, body, problems = build_request(op, loaded.compiled.get("defs") or {}, args)
    if problems:
        return {
            "error": "invalid_arguments",
            "invalid": problems[:10],
            "message": "参数不符合该操作的定义。用 systems_describe 查看参数后再调用。",
        }
    target = str(loaded.compiled.get("server") or system.base_url or "") + path
    # The catalog may predate a change of the system URL: never call the old origin.
    if not same_origin(target, system.base_url or ""):
        return context.unavailable(system)
    try:
        token = await context.proxy_token(ctx, system)
    except TokenProviderError as exc:
        return {
            "error": "token_unavailable",
            "reason": exc.code,
            "message": "平台没能为当前用户签发这个系统的令牌，请联系管理员检查签发方和用户授权。",
        }
    url = httpx.URL(target)
    if pairs:
        url = url.copy_with(params=pairs)
    headers = {"Accept": "application/json", **fetch.auth_headers(token)}
    started = time.monotonic()
    try:
        async with fetch.client_for(url, timeout=CALL_TIMEOUT) as client:
            async with client.stream(
                op["method"],
                url,
                headers=headers,
                **({"json": body} if body is not None else {}),
            ) as response:
                raw = bytearray()
                cut_off = False
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        cut_off = True
                        break
    except (fetch.FetchError, httpx.HTTPError, TimeoutError):
        elapsed = int((time.monotonic() - started) * 1000)
        await _record(
            ctx,
            system,
            args.operation_id,
            op,
            "unreachable",
            duration_ms=elapsed,
            token_id=token.token_id,
        )
        return {
            "error": "system_unreachable",
            "system": system.key,
            "message": "业务系统没有响应或连接失败。不要换别的方式重试同一个写操作，先告诉用户。",
        }
    elapsed = int((time.monotonic() - started) * 1000)
    result = shape(response, bytes(raw[:MAX_RESPONSE_BYTES]), cut_off, token.value)
    await _record(
        ctx,
        system,
        args.operation_id,
        op,
        "ok" if result["ok"] else "http_error",
        status=response.status_code,
        duration_ms=elapsed,
        token_id=token.token_id,
    )
    if response.status_code == 403:
        result["message"] = (
            "业务系统拒绝了当前用户（403）：按返回内容里的权限码提示用户去申请，不要换身份重试。"
        )
    return {"system": system.key, "operation_id": args.operation_id, **result, **DATA_TRUST}
