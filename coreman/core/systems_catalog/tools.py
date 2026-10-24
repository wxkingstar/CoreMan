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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coreman.core.auth.system_access import auth_label, auth_mode, token_env_var
from coreman.core.db.models import BusinessSystem
from coreman.core.systems_catalog import context, proxy, service
from coreman.core.systems_catalog.context import TRUST, Context
from coreman.core.systems_catalog.search import normalize

MAX_CALLS = 40
# Proxied calls have their own, smaller allowance: each one reaches the business system.
MAX_PROXY_CALLS = 30
MAX_REPEATS = 2
BROWSE_PAGE = 20
MODULE_PAGE = 25
GROUP_PAGE = 30
SEARCH_PAGE = 10
# A module page also stops at this many characters, so long module texts cannot swell it.
L1_MAX = 6000
L3_MAX = 6000
EXPAND_MAX = 12000
MODULE_TEXT = 120
# A longer guide is shown as an excerpt, with view="guide" for the full text.
GUIDE_INLINE = 600
GUIDE_EXCERPT = 200
# describe repeats a guide this short; a longer one is referenced.
GUIDE_IN_DETAIL = 300


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


SystemKey = Field(min_length=1, max_length=50, description="System key, e.g. stock")
Cursor = Field(default=None, max_length=20, description="next_cursor from the previous page")


class Browse(Arguments):
    system: str | None = Field(default=None, max_length=50, description="System key, e.g. stock")
    group: str | None = Field(default=None, max_length=100, description="Business group name")
    module: str | None = Field(default=None, max_length=200, description="Module name")
    view: Literal["groups", "modules", "guide"] | None = Field(
        default=None,
        description="With system only: groups (default when declared), modules (every module, "
        "paged) or guide (the system's full conventions)",
    )
    cursor: str | None = Cursor


class Search(Arguments):
    query: str = Field(min_length=1, max_length=200)
    system: str | None = Field(default=None, max_length=50, description="System key, e.g. stock")
    module: str | None = Field(default=None, max_length=200, description="Only this module")
    risk: Literal["read", "write", "destructive", "financial"] | None = None
    cursor: str | None = Cursor


class Describe(Arguments):
    system: str = SystemKey
    operation_id: str = Field(min_length=1, max_length=100)
    expand: Literal["parameters", "body", "response", "example"] | None = Field(
        default=None, description="Return this part in full when the detail lists it as omitted"
    )


MODELS: dict[str, type[BaseModel]] = {
    "systems_browse": Browse,
    "systems_search": Search,
    "systems_describe": Describe,
    "systems_call": proxy.Call,
}
DESCRIPTIONS = {
    "systems_browse": (
        'Overview of business systems, for questions like "what can this system do". To find '
        "the operation for a concrete task, use systems_search instead. No arguments: systems "
        "available this turn. With system: its business groups with module and operation counts "
        'and call rules (modules directly when it declares no groups); view="modules" pages '
        'through every module, view="guide" returns the system\'s full conventions. With '
        "group: that group's modules. With module: one-line operation summaries, 20 per page. "
        "Lists carry total and next_cursor; has_more=true means the list is incomplete."
    ),
    "systems_search": (
        "Default first step: find operations by keywords, operationId, module name or alias "
        "(Chinese or English) across this turn's systems, then systems_describe the one you "
        "pick. Optionally filter by system, module or risk. Best matches first, up to 10 per "
        "page, each with its module and why it matched; a marker such as [financial; 不代理] "
        "says the operation needs confirmation or cannot be called this turn."
    ),
    "systems_describe": (
        "Parameters, request body, response, permission, risk and how to call one operation. "
        "Check this before calling an operation. When `omitted` lists trimmed parts, call again "
        "with expand to get one of them in full."
    ),
    "systems_call": proxy.DESCRIPTION,
}


def tool_definitions(*, proxied: bool) -> list[dict[str, Any]]:
    """`proxied`: this turn has a platform-proxied system, so `systems_call` is listed too."""
    return [
        {
            "name": name,
            "description": DESCRIPTIONS[name],
            "inputSchema": _slim(model.model_json_schema()),
        }
        for name, model in MODELS.items()
        if proxied or name != "systems_call"
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
    counter, limit = (
        ("proxy_calls", MAX_PROXY_CALLS) if name == "systems_call" else ("calls", MAX_CALLS)
    )
    state[counter] = int(state.get(counter) or 0) + 1
    if state[counter] > limit:
        return {
            "error": "catalog_budget_exhausted",
            "stop": True,
            "message": f"本轮{'代理调用' if counter == 'proxy_calls' else '目录调用'}"
            f"已超过 {limit} 次。停止调用这些工具，用已经拿到的信息完成回复。",
        }
    if seen[fingerprint] > MAX_REPEATS:
        return {
            "error": "repeated_call",
            "stop": True,
            "message": "相同参数的目录调用已经重复多次，结果不会变化。"
            "不要再重复，直接使用已有结果。",
        }
    return None


CALL_NOTES = {"risk_not_allowed": "不代理", "write_not_allowed": "未开启写入"}


def call_note(ctx: Context, system: BusinessSystem, op: dict[str, Any]) -> str:
    """Whether this turn may call the operation, from the catalog's risk and the call mode."""
    if system.token_delivery == "proxy":
        denied = proxy.policy_error(ctx, system, op)
        return CALL_NOTES.get(denied or "", "")
    return "需确认" if op["risk"] in proxy.NEVER_PROXIED else ""


def call_rules(ctx: Context, system: BusinessSystem) -> str:
    if system.token_delivery == "proxy":
        writes = "已开启" if system.key in ctx.writable else "未开启，需管理员开启「允许写入」"
        return (
            f"平台代理：用 systems_call 调用。读操作直接执行；写操作{writes}；"
            "destructive、financial 不代理，需用户本人在业务系统里操作。"
        )
    return (
        "持令牌直接调用（按 systems_describe 的 curl 模板）。destructive、financial 先向当前发言者"
        "说明并等对方明确确认；定时任务中不执行。"
    )


def summary_line(op_id: str, op: dict[str, Any], note: str = "") -> str:
    line = f"{op_id}  {op['method']} {op['path']}  {op.get('summary') or ''}".rstrip()
    marks = [op["risk"]] if op.get("risk") and op["risk"] != "read" else []
    if note:
        marks.append(note)
    if marks:
        line += f"  [{'; '.join(marks)}]"
    if op.get("deprecated"):
        line += "  [deprecated]"
    return line


def permission_check(loaded: service.Loaded, held: service.Held | None) -> str:
    """filtered: lists show only what the member may use; unknown: the lookup failed, so nothing
    was filtered; not_declared: the system offers no lookup, being listed says nothing about
    permission. The business system checks every call either way."""
    if not isinstance(loaded.compiled.get("permissions"), dict):
        return "not_declared"
    return "unknown" if held is None else "filtered"


def _tag(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:8]


def _cursor(tag: str, offset: int) -> str:
    return f"{tag}.{offset}"


def _offset(cursor: str | None, tag: str) -> int | None:
    """Start of the page a cursor points at; None when it belongs to another catalog version.

    Bare numbers are cursors from before versioning and are taken as they are.
    """
    if not cursor:
        return 0
    if cursor.isdigit():
        return int(cursor)
    found, _, offset = cursor.partition(".")
    if found != tag or not offset.isdigit():
        return None
    return int(offset)


def _expired(system: str | None) -> dict[str, Any]:
    return {
        "error": "cursor_expired",
        "system": system,
        "message": "目录已更新，这个翻页位置不再有效。去掉 cursor 从第一页重新开始。",
    }


def _paged(items: list[Any], start: int, size: int, tag: str) -> dict[str, Any]:
    end = start + size
    more = end < len(items)
    return {
        "total": len(items),
        "has_more": more,
        "next_cursor": _cursor(tag, end) if more else None,
    }


def _module_item(module: dict[str, Any], visible: list[str], ops: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"name": module["name"]}
    if module.get("display_name"):
        item["display_name"] = module["display_name"]
    item["description"] = (module.get("description") or "")[:MODULE_TEXT]
    item["operations"] = len(visible)
    risks: dict[str, int] = {}
    for op_id in visible:
        if ops[op_id]["risk"] != "read":
            risks[ops[op_id]["risk"]] = risks.get(ops[op_id]["risk"], 0) + 1
    if risks:
        item["risks"] = risks
    if module.get("limits"):
        item["limits"] = module["limits"]
    return item


def _visible_modules(
    loaded: service.Loaded, held: service.Held | None
) -> list[tuple[dict[str, Any], list[str]]]:
    ops = loaded.compiled["operations"]
    out: list[tuple[dict[str, Any], list[str]]] = []
    for module in loaded.compiled["modules"]:
        visible = [op_id for op_id in module["operations"] if context.visible(ops[op_id], held)]
        if visible:
            out.append((module, visible))
    return out


def _module_named(loaded: service.Loaded, name: str) -> dict[str, Any]:
    modules: list[dict[str, Any]] = loaded.compiled["modules"]
    return next((m for m in modules if m["name"] == name), {})


def _find_module(loaded: service.Loaded, name: str) -> dict[str, Any] | None:
    """By stable name; otherwise by a display name or alias that names exactly one module."""
    found = _module_named(loaded, name)
    if found:
        return found
    modules: list[dict[str, Any]] = loaded.compiled["modules"]
    wanted = normalize(name)
    matches = [
        m
        for m in modules
        if wanted
        in {normalize(label) for label in [m.get("display_name") or "", *(m.get("aliases") or [])]}
    ]
    return matches[0] if len(matches) == 1 else None


async def browse_systems(ctx: Context) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for system in ctx.systems:
        item: dict[str, Any] = {
            "key": system.key,
            "name": system.name,
            "description": (system.description or "")[:MODULE_TEXT],
            "base_url": system.base_url or "",
            "call_mode": system.token_delivery,
        }
        loaded = await context.catalog(ctx, system) if system.openapi_url else None
        if loaded is None:
            item["catalog"] = "unavailable"
        else:
            held = await context.held(ctx, system, loaded)
            modules = _visible_modules(loaded, held)
            item.update(
                catalog=loaded.status,
                modules=len(modules),
                operations=sum(len(visible) for _, visible in modules),
                permission_check=permission_check(loaded, held),
            )
            names = {m["name"] for m, _ in modules}
            groups = [g for g in loaded.compiled.get("groups") or [] if names & set(g["modules"])]
            if groups:
                item["groups"] = len(groups)
            if context.permissions_unknown(loaded, held):
                item["permissions_unknown"] = True
        items.append(item)
    return {"systems": items, **TRUST}


def _guide(guide: str) -> dict[str, Any]:
    if len(guide) <= GUIDE_INLINE:
        return {"guide": guide}
    return {
        "guide": guide[:GUIDE_EXCERPT] + "…",
        "guide_truncated": True,
        "guide_chars": len(guide),
        "guide_hint": '系统说明较长，这里只是开头。用 view="guide" 查看全文。',
    }


async def browse_system(
    ctx: Context,
    system: BusinessSystem,
    *,
    view: str | None,
    group: str | None,
    cursor: str | None,
) -> dict[str, Any]:
    loaded = await context.catalog(ctx, system)
    if loaded is None:
        return context.unavailable(system)
    guide = loaded.compiled.get("guide") or ""
    if view == "guide":
        return {"system": system.key, "name": system.name, "guide": guide, **TRUST}
    held = await context.held(ctx, system, loaded)
    ops = loaded.compiled["operations"]
    modules = _visible_modules(loaded, held)
    by_name = {m["name"]: (m, visible) for m, visible in modules}
    groups = [
        (g, [by_name[name] for name in g["modules"] if name in by_name])
        for g in loaded.compiled.get("groups") or []
    ]
    groups = [(g, members) for g, members in groups if members]
    tag = _tag(loaded.sha, view or "", group or "")
    start = _offset(cursor, tag)
    if start is None:
        return _expired(system.key)
    result: dict[str, Any] = {
        "system": system.key,
        "name": system.name,
        "catalog": loaded.status,
        "call_mode": system.token_delivery,
        "call_rules": call_rules(ctx, system),
        "permission_check": permission_check(loaded, held),
        "total_modules": len(modules),
        "total_operations": sum(len(visible) for _, visible in modules),
    }
    if context.permissions_unknown(loaded, held):
        result["permissions_unknown"] = True
    if group is not None:
        chosen = next((members for g, members in groups if g["name"] == group), None)
        if chosen is None:
            return {
                "error": "group_not_found",
                "system": system.key,
                "groups": [g["name"] for g, _ in groups],
                "message": "没有这个业务分组。从 groups 里选一个，或用 systems_search 直接查找。",
                **TRUST,
            }
        listed, result["group"] = chosen, group
    elif view != "modules" and groups:
        page = groups[start : start + GROUP_PAGE]
        result.update(
            view="groups",
            **({"guide": ""} if start else _guide(guide)),
            groups=[
                {
                    "name": g["name"],
                    **({"description": g["description"]} if g.get("description") else {}),
                    "modules": len(members),
                    "operations": sum(len(visible) for _, visible in members),
                }
                for g, members in page
            ],
            **_paged(groups, start, GROUP_PAGE, tag),
            hint='传 group 查看组内模块；view="modules" 分页列出全部模块；'
            "找具体操作直接用 systems_search。",
        )
        return {**result, **TRUST}
    else:
        listed = modules
        result["view"] = "modules"
        if view == "groups":
            result["message"] = "这个系统没有声明业务分组，下面按模块列出。"
        result.update({"guide": ""} if start else _guide(guide))
    result["modules"] = []
    result.update(_paged(listed, start, MODULE_PAGE, tag))
    for index in range(start, min(start + MODULE_PAGE, len(listed))):
        module, visible = listed[index]
        result["modules"].append(_module_item(module, visible, ops))
        if len(json.dumps(result, ensure_ascii=False)) > L1_MAX and len(result["modules"]) > 1:
            result["modules"].pop()
            result.update(has_more=True, next_cursor=_cursor(tag, index))
            break
    return {**result, **TRUST}


async def browse_operations(
    ctx: Context, system: BusinessSystem, module_name: str, cursor: str | None
) -> dict[str, Any]:
    loaded = await context.catalog(ctx, system)
    if loaded is None:
        return context.unavailable(system)
    module = _find_module(loaded, module_name)
    if module is None:
        return {
            "error": "module_not_found",
            "system": system.key,
            "message": "没有这个模块。用 systems_browse 只传 system 查看模块列表。",
        }
    held = await context.held(ctx, system, loaded)
    ops = loaded.compiled["operations"]
    visible = [op_id for op_id in module["operations"] if context.visible(ops[op_id], held)]
    tag = _tag(loaded.sha, module["name"])
    start = _offset(cursor, tag)
    if start is None:
        return _expired(system.key)
    page = visible[start : start + BROWSE_PAGE]
    result: dict[str, Any] = {
        "system": system.key,
        "module": module["name"],
        **({"display_name": module["display_name"]} if module.get("display_name") else {}),
        "description": module.get("description") or "",
        **({"limits": module["limits"]} if module.get("limits") else {}),
        "operations": [
            summary_line(op_id, ops[op_id], call_note(ctx, system, ops[op_id])) for op_id in page
        ],
        **_paged(visible, start, BROWSE_PAGE, tag),
        "permission_check": permission_check(loaded, held),
    }
    if context.permissions_unknown(loaded, held):
        result["permissions_unknown"] = True
    return {**result, **TRUST}


async def search(ctx: Context, args: Search) -> dict[str, Any]:
    if args.system is not None:
        system = ctx.system(args.system)
        if system is None:
            return context.no_system(args.system)
        targets = [system]
    else:
        targets = [s for s in ctx.systems if s.openapi_url]
    hits: list[tuple[tuple[int, float, int, int, float], str, str, str]] = []
    unknown: list[str] = []
    unavailable: list[str] = []
    catalogs: dict[str, tuple[BusinessSystem, service.Loaded]] = {}
    module_found = args.module is None
    for system in targets:
        loaded = await context.catalog(ctx, system)
        if loaded is None:
            unavailable.append(system.key)
            continue
        catalogs[system.key] = (system, loaded)
        only: str | None = None
        if args.module is not None:
            module = _find_module(loaded, args.module)
            if module is None:
                continue
            only, module_found = module["name"], True
        held = await context.held(ctx, system, loaded)
        if context.permissions_unknown(loaded, held):
            unknown.append(system.key)
        ops = loaded.compiled["operations"]
        for hit in loaded.index.search(args.query):
            op = ops[hit.op_id]
            if (
                (only is None or op["module"] == only)
                and (args.risk is None or op["risk"] == args.risk)
                and context.visible(op, held)
            ):
                hits.append((hit.key, system.key, hit.op_id, hit.match))
    if args.system is not None and unavailable:
        return context.unavailable(targets[0])
    hits.sort(key=lambda item: (item[0], item[1], item[2]))
    tag = _tag(
        args.query,
        args.module or "",
        args.risk or "",
        *sorted(f"{key}:{loaded.sha}" for key, (_, loaded) in catalogs.items()),
    )
    start = _offset(args.cursor, tag)
    if start is None:
        return _expired(args.system)
    items: list[dict[str, str]] = []
    for _, key, op_id, match in hits[start : start + SEARCH_PAGE]:
        system, loaded = catalogs[key]
        op = loaded.compiled["operations"][op_id]
        module = _module_named(loaded, op["module"])
        item = {"system": key, "module": op["module"]}
        if module.get("display_name"):
            item["module_name"] = module["display_name"]
        item["operation"] = summary_line(op_id, op, call_note(ctx, system, op))
        item["match"] = match
        items.append(item)
    result: dict[str, Any] = {"items": items, **_paged(hits, start, SEARCH_PAGE, tag)}
    if unknown:
        result["permissions_unknown"] = unknown
    if unavailable:
        result["catalog_unavailable"] = unavailable
    if not module_found:
        result["message"] = "没有这个模块。去掉 module，或用 systems_browse 查看模块列表。"
    elif not hits:
        result["message"] = "没有匹配的操作。换个关键词，或用 systems_browse 按模块查看。"
    return {**result, **TRUST}


def call_template(
    ctx: Context, system: BusinessSystem, op: dict[str, Any], url: str
) -> dict[str, Any]:
    if system.token_delivery == "proxy":
        denied = proxy.policy_error(ctx, system, op)
        return {
            "mode": "proxy",
            "tool": "systems_call",
            "allowed": denied is None,
            "message": "这个系统由平台代理调用：用 systems_call 传 system、operation_id 和参数"
            "（path_params、query、body）。运行环境里没有这个系统的令牌，不要用 curl 直接调用。"
            + ("" if denied is None else proxy.DENIALS[denied]),
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


def _size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _fit(result: dict[str, Any]) -> dict[str, Any]:
    """Shrink a detail to L3_MAX characters: shallower schemas first, then drop parts.

    Method, path, risk, permissions, hint and the call template are never touched. Every part
    that was shortened is named in `omitted`, so it can be fetched with expand.
    """
    if _size(result) <= L3_MAX:
        return result
    trimmed = dict(result)
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
        if _size(trimmed) <= L3_MAX:
            break
    else:
        trimmed = {k: v for k, v in trimmed.items() if k not in ("response", "example")}
        trimmed["parameters"] = [dict(p) for p in trimmed.get("parameters") or []]
        for param in trimmed["parameters"]:
            param.pop("enum", None)
            if "description" in param:
                param["description"] = param["description"][:60]
        if _size(trimmed) > L3_MAX:
            trimmed.pop("body", None)
            trimmed["description"] = (trimmed.get("description") or "")[:300]
    omitted = [
        part
        for part in ("parameters", "body", "response", "example")
        if result.get(part) is not None and trimmed.get(part) != result.get(part)
    ]
    if trimmed.get("description") != result.get("description"):
        omitted.append("description")
    trimmed["truncated"] = True
    trimmed["omitted"] = omitted
    trimmed["omitted_hint"] = (
        "以上部分为控制长度做了精简或省略；需要时用 expand 取其中一项的完整内容"
        "（description 随 expand=parameters 一起返回）。"
    )
    return trimmed


def _expanded(header: dict[str, Any], part: str, value: Any) -> dict[str, Any]:
    result = {**header, "expand": part, part: value}
    if part in ("body", "response") and isinstance(value, dict) and "schema" in value:
        for depth in (4, 3, 2, 1, 0):
            if _size(result) <= EXPAND_MAX:
                break
            result[part] = {**value, "schema": _trim(value["schema"], depth)}
            result["truncated"] = True
    elif part == "example" and isinstance(value, str) and len(value) > EXPAND_MAX:
        result[part], result["truncated"] = value[:EXPAND_MAX], True
    return result


async def describe(
    ctx: Context, system: BusinessSystem, operation_id: str, expand: str | None = None
) -> dict[str, Any]:
    loaded = await context.catalog(ctx, system)
    if loaded is None:
        return context.unavailable(system)
    op = loaded.compiled["operations"].get(operation_id)
    held = await context.held(ctx, system, loaded) if op is not None else None
    if op is None or not context.visible(op, held):
        return context.operation_not_found(system, operation_id)
    url = str(loaded.compiled.get("server") or system.base_url or "").rstrip("/") + op["path"]
    detail = op["detail"]
    module = _module_named(loaded, op["module"])
    header: dict[str, Any] = {
        "system": system.key,
        "operation_id": operation_id,
        "module": op["module"],
    }
    if module.get("display_name"):
        header["module_name"] = module["display_name"]
    header.update(
        method=op["method"],
        path=op["path"],
        risk=op["risk"],
        permission=op.get("permission") or [],
        permission_check=permission_check(loaded, held),
    )
    if expand is not None:
        value: Any = (
            {
                "description": op.get("description") or "",
                "parameters": detail.get("parameters") or [],
            }
            if expand == "parameters"
            else detail.get(expand)
        )
        return {**_expanded(header, expand, value), **TRUST}
    result: dict[str, Any] = {
        **header,
        "url": url,
        "summary": op.get("summary") or "",
        "description": op.get("description") or "",
        "extra_permissions": op.get("extra_permissions") or [],
        "hint": op.get("hint") or "",
        "deprecated": bool(op.get("deprecated")),
        "parameters": [dict(p) for p in detail.get("parameters") or []],
        "body": detail.get("body"),
        "response": detail.get("response"),
        "example": detail.get("example"),
        "call": call_template(ctx, system, op, url),
    }
    if module.get("limits"):
        result["module_limits"] = module["limits"]
    guide = loaded.compiled.get("guide") or ""
    if guide and len(guide) <= GUIDE_IN_DETAIL:
        result["system_guide"] = guide
    elif guide:
        result["system_guide"] = (
            f"系统通用约定（时区、金额单位、分页等）共 {len(guide)} 字，未读过时用 "
            f'systems_browse(system="{system.key}", view="guide") 查看。'
        )
    if context.permissions_unknown(loaded, held):
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
            if args.module is not None or args.group is not None or args.view is not None:
                return {
                    "error": "invalid_tool_or_arguments",
                    "invalid": ["group, module and view need system"],
                }
            return await browse_systems(ctx)
        system = ctx.system(args.system)
        if system is None:
            return context.no_system(args.system)
        if args.module is None:
            return await browse_system(
                ctx, system, view=args.view, group=args.group, cursor=args.cursor
            )
        return await browse_operations(ctx, system, args.module, args.cursor)
    if isinstance(args, Search):
        return await search(ctx, args)
    if isinstance(args, proxy.Call):
        return await proxy.call(ctx, args)
    assert isinstance(args, Describe)
    system = ctx.system(args.system)
    if system is None:
        return context.no_system(args.system)
    return await describe(ctx, system, args.operation_id, args.expand)
