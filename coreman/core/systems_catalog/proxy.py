"""Proxied business system calls (`systems_call`) for systems with `token_delivery = proxy`.

The platform calls the system on the member's behalf, so the token never reaches the runtime.
Only operations in the catalog that the member can see are callable and arguments are validated
against the operation's schemas. Reads run at once. `write` and `destructive` need the AI
employee's grant to allow writes and are only staged: the member confirms them on a card and the
platform then runs exactly what was staged (see `confirm`). `financial` is never proxied. The
response is bounded and every call that reaches a policy decision is recorded without arguments,
bodies or the token.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from coreman.core.auth.token_providers import TokenProviderError
from coreman.core.db.models import BusinessSystem, SystemCall
from coreman.core.logging import get_logger
from coreman.core.object_store import MAX_SIZE as STORE_MAX
from coreman.core.object_store import disk_io
from coreman.core.systems_catalog import context, fetch, spill
from coreman.core.systems_catalog.compiler import same_origin
from coreman.core.systems_catalog.context import Context
from coreman.core.systems_catalog.validate import Validator, coerce

log = get_logger(__name__)

CALL_TIMEOUT = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RESULT_MAX = 100_000
ERROR_MAX = 4000
LIST_STEPS = (200, 100, 50, 20, 10, 5, 2, 1)
NEVER_PROXIED = ("financial",)
# Staged for the member's confirmation instead of running at once.
CONFIRMED_RISKS = ("write", "destructive")
# Header parameters the platform fills itself. A staged write keeps its value, so running it again
# reaches the business system with the same key.
IDEMPOTENCY_HEADERS = ("idempotency-key", "x-operation-request-id")
DATA_TRUST = {"content_trust": "system_returned_data"}
_TEXT_TYPES = ("json", "text/", "xml", "yaml", "javascript", "x-www-form-urlencoded")

Scalar = str | int | float | bool


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    field: str = Field(min_length=1, max_length=100)
    before: Scalar | None = Field(default=None, description="Current value, read it first")
    after: Scalar | None = None


class Note(BaseModel):
    """What the model says a write does; shown on the confirmation card as unverified text."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    target: str | None = Field(default=None, max_length=200, description="What is changed")
    changes: list[Change] | None = Field(default=None, max_length=10)
    impact: str | None = Field(default=None, max_length=300)
    rollback: str | None = Field(default=None, max_length=300, description="How to undo it")


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
    note: Note | None = Field(
        default=None,
        description="Writes only: the object, before/after values, impact and how to undo it, "
        "shown to the member on the confirmation card",
    )


DESCRIPTION = (
    "Call an operation of a system marked as platform-proxied, as the current member. Check it "
    "with systems_describe first. Reads run directly. Writes (write, destructive) need the AI "
    "employee's write permission and only join this turn's confirmation list: when the turn "
    "ends the member gets one card for all of them, and the platform runs them once confirmed. "
    "Financial operations are never proxied."
)


@dataclass
class Prepared:
    """A request that passed every check, ready to send or to stage for confirmation."""

    system: BusinessSystem
    operation_id: str
    op: dict[str, Any]
    url: httpx.URL
    body: Any
    # Idempotency header names the operation declares; `send` fills them.
    keys: list[str]


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
        elif where == "header" and name.lower() in IDEMPOTENCY_HEADERS:
            continue
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


def idempotency_headers(op: dict[str, Any]) -> list[str]:
    return [
        param["name"]
        for param in op["validate"]["params"]
        if param["in"] == "header" and param["name"].lower() in IDEMPOTENCY_HEADERS
    ]


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


async def record(
    ctx: Context,
    system: BusinessSystem,
    operation_id: str,
    op: dict[str, Any],
    outcome: str,
    *,
    status: int | None = None,
    duration_ms: int | None = None,
    token_id: str | None = None,
    object_id: uuid.UUID | None = None,
) -> None:
    ctx.session.add(
        SystemCall(
            task_id=ctx.member.task_id,
            bot_id=ctx.member.bot_id,
            user_id=ctx.member.user.id,
            system_key=system.key,
            operation_id=operation_id,
            method=op["method"],
            risk=op["risk"],
            outcome=outcome,
            status_code=status,
            duration_ms=duration_ms,
            token_id=token_id,
            object_id=object_id,
        )
    )
    await ctx.session.commit()
    log.info(
        "system_call",
        task_id=ctx.member.task_id,
        system=system.key,
        operation=operation_id,
        risk=op["risk"],
        outcome=outcome,
        status=status,
        duration_ms=duration_ms,
        token_id=token_id,
        object_id=object_id,
    )


def _spills(response: httpx.Response, head: bytes, whole: bool) -> bool:
    """A successful response too large to hand over inline, or a file."""
    if not 200 <= response.status_code < 300:
        return False
    if spill.is_file(response) or not whole:
        return True
    return len(head) > spill.INLINE_MAX and (
        len(head.decode("utf-8", errors="replace")) > spill.INLINE_MAX
    )


def policy_error(ctx: Context, system: BusinessSystem, op: dict[str, Any]) -> str | None:
    """Why this operation may not be proxied for this AI employee, or None."""
    if op["risk"] in NEVER_PROXIED:
        return "risk_not_allowed"
    if op["risk"] in CONFIRMED_RISKS and ctx.owner_absent:
        return "owner_turn_required"
    if op["risk"] in CONFIRMED_RISKS and system.key not in ctx.writable:
        return "write_not_allowed"
    return None


DENIALS = {
    "risk_not_allowed": "涉及资金的操作（financial）不通过平台代理执行。"
    "请告诉用户需要本人在业务系统里操作。",
    "write_not_allowed": "这个 AI 员工没有该系统的写入权限。需要管理员在 AI 员工的「系统权限」里"
    "为这个系统开启「允许写入」；在此之前不要尝试其他写入方式。",
    "owner_turn_required": "这一轮是个人助理在所有者不在场时跑的，业务系统只能查询，不能做写操作；"
    "把要做的写操作写进给所有者的结论里，由所有者在私聊里亲自让助理去做。",
}


async def prepare(ctx: Context, args: Call) -> Prepared | dict[str, Any]:
    """Every check a call passes before it may reach the business system; an error otherwise."""
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
        await record(ctx, system, args.operation_id, op, "denied")
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
    url = httpx.URL(target)
    if pairs:
        url = url.copy_with(params=pairs)
    return Prepared(system, args.operation_id, op, url, body, idempotency_headers(op))


async def send(
    ctx: Context, p: Prepared, *, key: str | None = None, prefix: str = ""
) -> dict[str, Any]:
    """Call the business system as the member.

    `key` fills the idempotency headers (a fresh value when not given); `prefix` marks the
    recorded outcome, e.g. `confirmed_` for a write run from its confirmation card.
    """
    system, op = p.system, p.op
    try:
        token = await context.proxy_token(ctx, system)
    except TokenProviderError as exc:
        return {
            "error": "token_unavailable",
            "reason": exc.code,
            "message": "平台没能为当前用户签发这个系统的令牌，请联系管理员检查签发方和用户授权。",
        }
    headers = {"Accept": "application/json", **fetch.auth_headers(token)}
    value = key or str(uuid.uuid4())
    headers.update({name: value for name in p.keys})
    # With storage, a large body is read in full and kept as a file; otherwise only what fits.
    limit = STORE_MAX if ctx.settings is not None else MAX_RESPONSE_BYTES
    started = time.monotonic()
    with tempfile.SpooledTemporaryFile(max_size=MAX_RESPONSE_BYTES) as spool:
        try:
            async with fetch.client_for(p.url, timeout=CALL_TIMEOUT) as client:
                async with client.stream(
                    op["method"],
                    p.url,
                    headers=headers,
                    **({"json": p.body} if p.body is not None else {}),
                ) as response:
                    size = 0
                    cut_off = False
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > limit:
                            cut_off = True
                            break
                        await disk_io(partial(spool.write, part))
        except (fetch.FetchError, httpx.HTTPError, TimeoutError):
            elapsed = int((time.monotonic() - started) * 1000)
            await record(
                ctx,
                system,
                p.operation_id,
                op,
                prefix + "unreachable",
                duration_ms=elapsed,
                token_id=token.token_id,
            )
            return {
                "error": "system_unreachable",
                "system": system.key,
                "message": "业务系统没有响应或连接失败。"
                "不要换别的方式重试同一个写操作，先告诉用户。",
            }
        elapsed = int((time.monotonic() - started) * 1000)
        await disk_io(partial(spool.seek, 0))
        head = await disk_io(partial(spool.read, MAX_RESPONSE_BYTES))
        whole = not cut_off and size <= MAX_RESPONSE_BYTES
        stored = None
        if ctx.settings is not None and not cut_off and _spills(response, head, whole):
            stored = await spill.store(
                ctx,
                system_key=system.key,
                operation_id=p.operation_id,
                response=response,
                spool=spool,
                head=head,
                complete=whole,
                secret=token.value,
            )
    if stored is not None:
        result, object_id = stored
    else:
        result, object_id = shape(response, head, not whole, token.value), None
        if cut_off and ctx.settings is not None and result.get("ok"):
            result["hint"] = (
                f"响应超过 {STORE_MAX // (1024 * 1024)} MB，没有保存。请用分页、筛选或时间范围参数"
                "缩小结果后再查。"
            )
    await record(
        ctx,
        system,
        p.operation_id,
        op,
        prefix + ("ok" if result["ok"] else "http_error"),
        status=response.status_code,
        duration_ms=elapsed,
        token_id=token.token_id,
        object_id=object_id,
    )
    if response.status_code == 403:
        result["message"] = (
            "业务系统拒绝了当前用户（403）：按返回内容里的权限码提示用户去申请，不要换身份重试。"
        )
    return {"system": system.key, "operation_id": p.operation_id, **result, **DATA_TRUST}


async def call(ctx: Context, args: Call) -> dict[str, Any]:
    prepared = await prepare(ctx, args)
    if isinstance(prepared, dict):
        return prepared
    if prepared.op["risk"] in CONFIRMED_RISKS and not ctx.writes_preapproved:
        from coreman.core.systems_catalog import confirm

        return await confirm.propose(ctx, prepared, args)
    return await send(ctx, prepared)
