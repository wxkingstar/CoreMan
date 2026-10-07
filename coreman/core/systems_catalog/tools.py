"""Layered catalog tools: systems → modules → operations → detail.

Every result is bounded (pages, character budgets) and marked as system-declared text: summaries
and descriptions come from the business system and describe its API, they are not instructions.
Operations the member cannot use are left out when the system declares a permission lookup; an
operation that is missing or not visible is reported as such, never replaced by a similar one.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import auth_label, auth_mode, token_env_var
from coreman.core.auth.token_providers import TokenProviderError
from coreman.core.db.models import BusinessSystem
from coreman.core.systems_catalog import service

MAX_CALLS = 40
MAX_REPEATS = 2
BROWSE_PAGE = 20
SEARCH_PAGE = 10
L1_MAX = 4000
L3_MAX = 6000
MODULE_TEXT = 120
# A failed permission lookup is retried after this long instead of on every call.
PERMISSIONS_RETRY_SECONDS = 60
TRUST = {"content_trust": "system_declared"}
BUDGET_KEY = "systems_catalog"


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


SystemKey = Field(min_length=1, max_length=50, description="System key, e.g. stock")
Cursor = Field(default=None, max_length=20, description="next_cursor from the previous page")


class Browse(Arguments):
    system: str | None = Field(default=None, max_length=50, description="System key, e.g. stock")
    module: str | None = Field(default=None, max_length=200)
    cursor: str | None = Cursor


class Search(Arguments):
    query: str = Field(min_length=1, max_length=200)
    system: str | None = Field(default=None, max_length=50, description="System key, e.g. stock")
    risk: Literal["read", "write", "destructive", "financial"] | None = None
    cursor: str | None = Cursor


class Describe(Arguments):
    system: str = SystemKey
    operation_id: str = Field(min_length=1, max_length=100)


MODELS: dict[str, type[Arguments]] = {
    "systems_browse": Browse,
    "systems_search": Search,
    "systems_describe": Describe,
}
DESCRIPTIONS = {
    "systems_browse": (
        "Browse business system operations level by level. No arguments: systems available this "
        "turn. With system: its guide and modules. With system and module: one-line summaries of "
        "the module's operations, 20 per page."
    ),
    "systems_search": (
        "Search operations by keywords (Chinese or English) across this turn's systems, or one "
        "system; optionally filter by risk. Returns one-line summaries, up to 10 per page."
    ),
    "systems_describe": (
        "Parameters, request body, response, permission, risk and how to call one operation. "
        "Check this before calling an operation."
    ),
}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": DESCRIPTIONS[name],
            "inputSchema": _slim(model.model_json_schema()),
        }
        for name, model in MODELS.items()
    ]


def _slim(value: Any) -> Any:
    """Drop titles and the `anyOf: [X, null]` wrapping optional fields; the server validates."""
    if isinstance(value, list):
        return [_slim(item) for item in value]
    if not isinstance(value, dict):
        return value
    options = value.get("anyOf")
    if isinstance(options, list) and len(options) == 2 and {"type": "null"} in options:
        rest = {
            k: v for k, v in value.items() if k != "anyOf" and not (k == "default" and v is None)
        }
        value = {**next(o for o in options if o != {"type": "null"}), **rest}
    return {
        key: _slim(item)
        for key, item in value.items()
        if not (key == "title" and isinstance(item, str))
        and not (key == "default" and item is None)
    }


@dataclass
class Context:
    """One verified call: the member, their systems this turn and the task's catalog state."""

    session: AsyncSession
    issuance: service.Issuance
    operator: service.Operator
    systems: list[BusinessSystem]
    # task.payload[BUDGET_KEY]["perms"]; the caller persists changes after the call.
    perms: dict[str, Any] = field(default_factory=dict)
    perms_changed: bool = False

    def system(self, key: str) -> BusinessSystem | None:
        return next((s for s in self.systems if s.key == key), None)


def charge(state: dict[str, Any], name: str, arguments: Any) -> dict[str, Any] | None:
    """Count one call in `state` (the task payload section); the stop result when over budget."""
    try:
        canonical: Any = MODELS[name].model_validate(arguments).model_dump()
    except (KeyError, ValidationError):
        canonical = arguments
    fingerprint = hashlib.sha256(
        json.dumps([name, canonical], sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:32]
    seen = dict(state.get("seen") or {})
    seen[fingerprint] = seen.get(fingerprint, 0) + 1
    state["seen"] = seen
    state["calls"] = int(state.get("calls") or 0) + 1
    if state["calls"] > MAX_CALLS:
        return {
            "error": "catalog_budget_exhausted",
            "stop": True,
            "message": f"本轮目录调用已超过 {MAX_CALLS} 次。"
            "停止调用目录工具，用已经拿到的信息完成回复。",
        }
    if seen[fingerprint] > MAX_REPEATS:
        return {
            "error": "repeated_call",
            "stop": True,
            "message": "相同参数的目录调用已经重复多次，结果不会变化。"
            "不要再重复，直接使用已有结果。",
        }
    return None


def summary_line(op_id: str, op: dict[str, Any]) -> str:
    line = f"{op_id}  {op['method']} {op['path']}  {op.get('summary') or ''}".rstrip()
    if op.get("risk") and op["risk"] != "read":
        line += f"  [{op['risk']}]"
    if op.get("deprecated"):
        line += "  [deprecated]"
    return line


def _offset(cursor: str | None) -> int:
    return int(cursor) if cursor and cursor.isdigit() else 0


async def _held(
    ctx: Context, system: BusinessSystem, loaded: service.Loaded
) -> service.Held | None:
    """The member's permission codes for this system, cached in the task; None = no filtering."""
    if not isinstance(loaded.compiled.get("permissions"), dict):
        return None
    cached = ctx.perms.get(system.key)
    if isinstance(cached, dict):
        if isinstance(cached.get("codes"), list):
            return service.Held(cached["codes"])
        if cached.get("until", 0) > time.time():
            return None
    codes: list[str] | None = None
    try:
        token = await service.issue_short_token(ctx.session, ctx.issuance, system, ctx.operator)
        await ctx.session.commit()
        codes = await service.held_permissions(loaded, system.base_url or "", token)
    except TokenProviderError:
        codes = None
    ctx.perms[system.key] = (
        {"codes": codes}
        if codes is not None
        else {"until": time.time() + PERMISSIONS_RETRY_SECONDS}
    )
    ctx.perms_changed = True
    return service.Held(codes) if codes is not None else None


def _permissions_unknown(loaded: service.Loaded, held: service.Held | None) -> bool:
    return isinstance(loaded.compiled.get("permissions"), dict) and held is None


def _visible(op: dict[str, Any], held: service.Held | None) -> bool:
    return held is None or held.allows(op.get("permission") or [])


async def _catalog(ctx: Context, system: BusinessSystem) -> service.Loaded | None:
    await service.ensure_fresh(ctx.session, ctx.issuance, system, ctx.operator)
    return await service.load(ctx.session, system.key)


def _unavailable(system: BusinessSystem) -> dict[str, Any]:
    return {
        "error": "catalog_unavailable",
        "system": system.key,
        "message": "这个系统暂时没有可用的操作目录（未配置或拉取失败）。"
        "不要猜路径；请告知用户目录不可用，或按系统已有的说明处理。",
    }


def _no_system(key: str) -> dict[str, Any]:
    return {
        "error": "system_not_available",
        "system": key,
        "message": "本轮没有这个业务系统的访问权限。不带参数调用 systems_browse 查看可用系统。",
    }


async def browse_systems(ctx: Context) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for system in ctx.systems:
        item: dict[str, Any] = {
            "key": system.key,
            "name": system.name,
            "description": (system.description or "")[:MODULE_TEXT],
            "base_url": system.base_url or "",
        }
        loaded = await _catalog(ctx, system) if system.openapi_url else None
        if loaded is None:
            item["catalog"] = "unavailable"
        else:
            held = await _held(ctx, system, loaded)
            ops = loaded.compiled["operations"]
            visible = [m for m in ops if _visible(ops[m], held)]
            item.update(
                catalog=loaded.status,
                modules=len({ops[m]["module"] for m in visible}),
                operations=len(visible),
            )
            if _permissions_unknown(loaded, held):
                item["permissions_unknown"] = True
        items.append(item)
    return {"systems": items, **TRUST}


async def browse_modules(
    ctx: Context, system: BusinessSystem, cursor: str | None
) -> dict[str, Any]:
    loaded = await _catalog(ctx, system)
    if loaded is None:
        return _unavailable(system)
    held = await _held(ctx, system, loaded)
    ops = loaded.compiled["operations"]
    modules: list[dict[str, Any]] = []
    for module in loaded.compiled["modules"]:
        count = sum(1 for op_id in module["operations"] if _visible(ops[op_id], held))
        if count:
            modules.append(
                {
                    "name": module["name"],
                    "description": (module.get("description") or "")[:MODULE_TEXT],
                    "operations": count,
                }
            )
    start = _offset(cursor)
    result: dict[str, Any] = {
        "system": system.key,
        "name": system.name,
        "guide": (loaded.compiled.get("guide") or "") if start == 0 else "",
        "catalog": loaded.status,
        "modules": [],
        "next_cursor": None,
    }
    if _permissions_unknown(loaded, held):
        result["permissions_unknown"] = True
    for index in range(start, len(modules)):
        result["modules"].append(modules[index])
        if len(json.dumps(result, ensure_ascii=False)) > L1_MAX and len(result["modules"]) > 1:
            result["modules"].pop()
            result["next_cursor"] = str(index)
            break
    return {**result, **TRUST}


async def browse_operations(
    ctx: Context, system: BusinessSystem, module_name: str, cursor: str | None
) -> dict[str, Any]:
    loaded = await _catalog(ctx, system)
    if loaded is None:
        return _unavailable(system)
    module = next((m for m in loaded.compiled["modules"] if m["name"] == module_name), None)
    if module is None:
        return {
            "error": "module_not_found",
            "system": system.key,
            "message": "没有这个模块。用 systems_browse 只传 system 查看模块列表。",
        }
    held = await _held(ctx, system, loaded)
    ops = loaded.compiled["operations"]
    visible = [op_id for op_id in module["operations"] if _visible(ops[op_id], held)]
    start = _offset(cursor)
    page = visible[start : start + BROWSE_PAGE]
    result: dict[str, Any] = {
        "system": system.key,
        "module": module["name"],
        "description": module.get("description") or "",
        "operations": [summary_line(op_id, ops[op_id]) for op_id in page],
        "next_cursor": str(start + BROWSE_PAGE) if start + BROWSE_PAGE < len(visible) else None,
    }
    if _permissions_unknown(loaded, held):
        result["permissions_unknown"] = True
    return {**result, **TRUST}


async def search(ctx: Context, args: Search) -> dict[str, Any]:
    if args.system is not None:
        system = ctx.system(args.system)
        if system is None:
            return _no_system(args.system)
        targets = [system]
    else:
        targets = [s for s in ctx.systems if s.openapi_url]
    scored: list[tuple[float, str, str]] = []
    unknown: list[str] = []
    unavailable: list[str] = []
    for system in targets:
        loaded = await _catalog(ctx, system)
        if loaded is None:
            unavailable.append(system.key)
            continue
        held = await _held(ctx, system, loaded)
        if _permissions_unknown(loaded, held):
            unknown.append(system.key)
        ops = loaded.compiled["operations"]
        for op_id, score in loaded.index.search(args.query):
            op = ops[op_id]
            if (args.risk is None or op["risk"] == args.risk) and _visible(op, held):
                scored.append((score, system.key, op_id))
    if args.system is not None and unavailable:
        return _unavailable(targets[0])
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    start = _offset(args.cursor)
    page = scored[start : start + SEARCH_PAGE]
    lines: list[dict[str, str]] = []
    for _, key, op_id in page:
        loaded = await service.load(ctx.session, key)
        if loaded is not None:
            lines.append(
                {
                    "system": key,
                    "operation": summary_line(op_id, loaded.compiled["operations"][op_id]),
                }
            )
    result: dict[str, Any] = {
        "items": lines,
        "next_cursor": str(start + SEARCH_PAGE) if start + SEARCH_PAGE < len(scored) else None,
    }
    if unknown:
        result["permissions_unknown"] = unknown
    if unavailable:
        result["catalog_unavailable"] = unavailable
    if not scored:
        result["message"] = "没有匹配的操作。换个关键词，或用 systems_browse 按模块查看。"
    return {**result, **TRUST}


def call_template(system: BusinessSystem, op: dict[str, Any], url: str) -> dict[str, Any]:
    if system.token_delivery == "proxy":
        return {
            "mode": "proxy",
            "tool": "systems_call",
            "message": "这个系统由平台代理调用：用 systems_call 传 system、operation_id 和参数。"
            "运行环境里没有这个系统的令牌，不要用 curl 直接调用。",
        }
    env_var = token_env_var(system.key)
    mode = auth_mode(system)
    header = (
        f"Authorization: Bearer ${env_var}" if mode == "bearer" else f"Cookie: bot_token=${env_var}"
    )
    parts = ["curl -sS -X", op["method"], shlex.quote(url), "-H", f'"{header}"']
    body = op["detail"].get("body")
    if body:
        content_type = body.get("content_type") or "application/json"
        parts += ["-H", shlex.quote(f"Content-Type: {content_type}"), "-d", "'<请求体>'"]
    return {
        "mode": "env",
        "token_env": env_var,
        "auth": auth_label(mode),
        "curl": " ".join(parts),
        "notes": "替换路径中的 {参数}，查询参数拼在 URL 上；令牌只用变量引用，不要写出它的值。",
    }


def _trim(schema: Any, depth: int) -> Any:
    if not isinstance(schema, dict):
        return schema
    nested = ("properties", "items", "oneOf", "anyOf", "additionalProperties")
    if depth <= 0 and any(key in schema for key in nested):
        return {
            **{k: v for k, v in schema.items() if k not in nested and k != "required"},
            "truncated": True,
        }
    out = dict(schema)
    if isinstance(schema.get("properties"), dict):
        out["properties"] = {k: _trim(v, depth - 1) for k, v in schema["properties"].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            out[key] = _trim(schema[key], depth - 1)
    for key in ("oneOf", "anyOf"):
        if isinstance(schema.get(key), list):
            out[key] = [_trim(v, depth - 1) for v in schema[key]]
    return out


def _fit(result: dict[str, Any]) -> dict[str, Any]:
    """Shrink a detail to L3_MAX characters: shallower schemas first, then drop parts."""

    def size(value: dict[str, Any]) -> int:
        return len(json.dumps(value, ensure_ascii=False))

    if size(result) <= L3_MAX:
        return result
    for body_depth, response_depth in ((3, 2), (2, 1), (1, 0), (0, 0)):
        trimmed = dict(result)
        if isinstance(result.get("body"), dict):
            trimmed["body"] = {
                **result["body"],
                "schema": _trim(result["body"].get("schema"), body_depth),
            }
        if isinstance(result.get("response"), dict) and "schema" in result["response"]:
            trimmed["response"] = {
                **result["response"],
                "schema": _trim(result["response"]["schema"], response_depth),
            }
        trimmed["truncated"] = True
        if size(trimmed) <= L3_MAX:
            return trimmed
    trimmed = {k: v for k, v in trimmed.items() if k not in ("response", "example")}
    for param in trimmed.get("parameters") or []:
        param.pop("enum", None)
        if "description" in param:
            param["description"] = param["description"][:60]
    if size(trimmed) > L3_MAX:
        trimmed.pop("body", None)
        trimmed["description"] = (trimmed.get("description") or "")[:300]
    return trimmed


async def describe(ctx: Context, system: BusinessSystem, operation_id: str) -> dict[str, Any]:
    loaded = await _catalog(ctx, system)
    if loaded is None:
        return _unavailable(system)
    op = loaded.compiled["operations"].get(operation_id)
    held = await _held(ctx, system, loaded) if op is not None else None
    if op is None or not _visible(op, held):
        return {
            "error": "operation_not_found",
            "system": system.key,
            "operation_id": operation_id,
            "message": "目录中没有这个操作，或当前用户没有权限使用它。"
            "用 systems_search 查找，不要猜测路径或改用相近的操作。",
        }
    url = str(loaded.compiled.get("server") or system.base_url or "").rstrip("/") + op["path"]
    detail = op["detail"]
    result: dict[str, Any] = {
        "system": system.key,
        "operation_id": operation_id,
        "module": op["module"],
        "method": op["method"],
        "path": op["path"],
        "url": url,
        "summary": op.get("summary") or "",
        "description": op.get("description") or "",
        "risk": op["risk"],
        "permission": op.get("permission") or [],
        "extra_permissions": op.get("extra_permissions") or [],
        "hint": op.get("hint") or "",
        "deprecated": bool(op.get("deprecated")),
        "parameters": [dict(p) for p in detail.get("parameters") or []],
        "body": detail.get("body"),
        "response": detail.get("response"),
        "example": detail.get("example"),
        "call": call_template(system, op, url),
    }
    if _permissions_unknown(loaded, held):
        result["permissions_unknown"] = True
    return {**_fit(result), **TRUST}


async def dispatch(ctx: Context, name: str, arguments: Any) -> dict[str, Any]:
    model = MODELS.get(name)
    if model is None or not isinstance(arguments, dict):
        return {"error": "invalid_tool_or_arguments"}
    try:
        args = model.model_validate(arguments)
    except ValidationError as exc:
        problems = [
            ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
            for error in exc.errors(include_url=False, include_input=False)[:5]
        ]
        return {"error": "invalid_tool_or_arguments", "invalid": problems}
    if isinstance(args, Browse):
        if args.system is None:
            if args.module is not None:
                return {"error": "invalid_tool_or_arguments", "invalid": ["module: needs system"]}
            return await browse_systems(ctx)
        system = ctx.system(args.system)
        if system is None:
            return _no_system(args.system)
        if args.module is None:
            return await browse_modules(ctx, system, args.cursor)
        return await browse_operations(ctx, system, args.module, args.cursor)
    if isinstance(args, Search):
        return await search(ctx, args)
    assert isinstance(args, Describe)
    system = ctx.system(args.system)
    if system is None:
        return _no_system(args.system)
    return await describe(ctx, system, args.operation_id)
