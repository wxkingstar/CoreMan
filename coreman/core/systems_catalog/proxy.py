"""Proxied business system calls (`systems_call`) for systems with `token_delivery = proxy`.

The platform calls the system on the member's behalf, so the token never reaches the runtime.
Only operations in the catalog that the member can see are callable and arguments are validated
against the operation's schemas. Reads run at once. `write`, `destructive` and `financial` need
the AI employee's grant to allow writes and are only staged: the member confirms them on a card and
the platform then runs exactly what was staged (see `confirm`). A `financial` operation is staged
one at a time, alone on its card, and never in a batch or without a card. The
response is bounded and every call that reaches a policy decision is recorded without arguments,
bodies or the token.

In a personal assistant's unattended turn nobody can click a card: a `write` the grant allows is
staged for the owner's approval in the approval system instead (`assistants.approvals.proxied`)
and, once approved, run by the platform through `prepare` and `send` with the approved request;
`destructive` stays refused there.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from coreman.core.auth.token_providers import TokenProviderError
from coreman.core.config import get_settings
from coreman.core.db.models import BusinessSystem, StoredObject, SystemCall
from coreman.core.logging import get_logger
from coreman.core.object_store import MAX_SIZE as STORE_MAX
from coreman.core.object_store import disk_io
from coreman.core.systems_catalog import context, fetch, spill, uploads
from coreman.core.systems_catalog.compiler import same_origin
from coreman.core.systems_catalog.context import Context
from coreman.core.systems_catalog.validate import Validator, coerce

log = get_logger(__name__)

CALL_TIMEOUT = 30
# Reads may be slow queries (reports, analytics); writes keep the shorter limit.
READ_TIMEOUT = 60
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RESULT_MAX = 100_000
ERROR_MAX = 4000
LIST_STEPS = (200, 100, 50, 20, 10, 5, 2, 1)
# Staged for the member's confirmation instead of running at once.
CONFIRMED_RISKS = ("write", "destructive", "financial")
# Confirmed one at a time: never batched, never run without a card, alone on its card.
SINGLE_RISKS = ("financial",)
# Writes a turn may run without a confirmation card once the owner approved the turn in advance
# (a personal assistant's dispatch with `allow_write`): as many as one card holds.
PREAPPROVED_WRITES_MAX = 50
# Header parameters the platform fills itself. A staged write keeps its value, so running it again
# reaches the business system with the same key.
IDEMPOTENCY_HEADERS = ("idempotency-key", "x-operation-request-id")
# Header parameters the caller may never set: the platform owns them.
RESERVED_HEADERS = (
    "authorization",
    "cookie",
    "host",
    "content-type",
    "content-length",
    "transfer-encoding",
    "connection",
    "accept",
)
HEADER_VALUE_MAX = 1000
# The file of an operation whose request body is a whole file, in `Prepared.files`.
RAW_FIELD = "body"
DATA_TRUST = {"content_trust": "system_returned_data"}
# Failures that happen before the request leaves: it certainly did not reach the business system.
NOT_SENT = (
    fetch.FetchError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)
_TEXT_TYPES = ("json", "text/", "xml", "yaml", "javascript", "x-www-form-urlencoded")

Scalar = str | int | float | bool


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    field: str = Field(
        min_length=1,
        max_length=100,
        description="Business name the member knows (e.g. 价格), not the API field name",
    )
    before: Scalar | None = Field(default=None, description="Current value, read it first")
    after: Scalar | None = None


class Note(BaseModel):
    """What the model says a write does; shown on the confirmation card as unverified text."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    target: str | None = Field(
        default=None,
        max_length=200,
        description="What is changed, in words a non-technical member recognises",
    )
    changes: list[Change] | None = Field(default=None, max_length=10)
    impact: str | None = Field(default=None, max_length=300)
    rollback: str | None = Field(default=None, max_length=300, description="How to undo it")


class BatchRef(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    upload_id: str = Field(min_length=1, max_length=100)


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
    headers: dict[str, Scalar] | None = Field(
        default=None,
        description="Header parameters the operation declares, e.g. If-Match",
    )
    body: Any = Field(
        default=None,
        description="Request body. For an upload or import operation, each file field takes "
        '{"upload_id": ...} from uploading the file first (see systems_describe)',
    )
    note: Note | None = Field(
        default=None,
        description="Writes only: the object, before/after values, impact and how to undo it, "
        "shown to the member on the confirmation card",
    )
    batch: BatchRef | None = Field(
        default=None,
        description="Writes only: many items of this one operation in one call, read from an "
        "uploaded JSON Lines file (one item per line, see systems_describe). Replaces "
        "path_params, query and body; note gives the impact and rollback of the whole batch",
    )


DESCRIPTION = (
    "Call an operation of a system marked as platform-proxied, as the current member. Check it "
    "with systems_describe first. Reads run directly. Writes (write, destructive) need the AI "
    "employee's write permission and only join this turn's confirmation list: when the turn "
    "ends the member gets one card for all of them, and the platform runs them once confirmed. "
    "A financial operation is confirmed on a card of its own, one at a time, never in a batch. "
    "Operations that take files: upload each file first "
    "as systems_describe shows and pass its upload_id. Many items of one write operation: put "
    "them in a JSON Lines file, upload it and pass batch, one call for the whole batch."
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
    # Multipart operations: (field, upload) for every file, in the order given.
    files: list[tuple[str, StoredObject]] = field(default_factory=list)
    # Header parameters the operation declares, as given (e.g. If-Match).
    headers: dict[str, str] = field(default_factory=dict)


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
        elif where == "header" and name.lower() not in RESERVED_HEADERS:
            continue  # idempotency keys are filled by `send`, the rest by `build_headers`
        elif param["required"]:
            problems.append(f"{name}: {where} parameters are not supported by systems_call")
    problems += [f"path_params.{name}: unknown parameter" for name in given_path]
    problems += [f"query.{name}: unknown parameter" for name in given_query]
    body_spec = spec["body"]
    if body_spec is None:
        if args.body is not None:
            problems.append("body: this operation takes no request body")
    elif body_spec.get("multipart"):
        problems += _form_problems(args.body, body_spec, validator)
    elif body_spec.get("raw"):
        if args.body is None:
            if body_spec["required"]:
                problems.append('body: required, pass {"upload_id": ...}')
        elif not uploads.is_ref(args.body):
            problems.append('body: the request body is a whole file, pass {"upload_id": ...}')
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


def _control(ch: str) -> bool:
    return ord(ch) < 32 or ord(ch) == 127


def build_headers(
    op: dict[str, Any], defs: dict[str, Any], args: Call
) -> tuple[dict[str, str], list[str]]:
    """(headers, problems): the header parameters the operation declares, checked like the rest."""
    validator = Validator(defs)
    declared = {
        param["name"].lower(): param
        for param in op["validate"]["params"]
        if param["in"] == "header"
        and param["name"].lower() not in IDEMPOTENCY_HEADERS
        and param["name"].lower() not in RESERVED_HEADERS
    }
    given = {str(name).lower(): (name, value) for name, value in (args.headers or {}).items()}
    problems = [
        f"headers.{name}: unknown parameter"
        for key, (name, _) in given.items()
        if key not in declared
    ]
    headers: dict[str, str] = {}
    for key, param in declared.items():
        name = param["name"]
        if key not in given:
            if param["required"]:
                problems.append(f"headers.{name}: required")
            continue
        value = coerce(given[key][1], param["schema"], validator)
        problems += validator.errors(value, param["schema"], f"headers.{name}")
        text = _text(value)
        if not text or len(text) > HEADER_VALUE_MAX or any(_control(ch) for ch in text):
            limit = HEADER_VALUE_MAX
            problems.append(f"headers.{name}: must be one line of at most {limit} characters")
            continue
        headers[name] = text
    return headers, problems


def _form_problems(body: Any, spec: dict[str, Any], validator: Validator) -> list[str]:
    """A multipart form: file fields hold upload references, the rest is checked like JSON."""
    if body is None:
        return ["body: required"] if spec["required"] else []
    if not isinstance(body, dict):
        return ["body: must be an object of form fields"]
    problems: list[str] = []
    for name, file in spec["files"].items():
        value = body.get(name)
        if value is None:
            if file["required"]:
                problems.append(f'body.{name}: required, pass {{"upload_id": ...}}')
            continue
        refs = value if file["many"] and isinstance(value, list) else [value]
        if not refs or not all(uploads.is_ref(ref) for ref in refs):
            shape = '[{"upload_id": ...}]' if file["many"] else '{"upload_id": ...}'
            problems.append(f"body.{name}: a file field takes {shape}")
    fields = {k: v for k, v in body.items() if k not in spec["files"]}
    return problems + validator.errors(fields, spec["schema"], "body")


def takes_files(op: dict[str, Any]) -> bool:
    """The operation is sent with uploaded files: a multipart form or a whole-file body."""
    body = op["validate"].get("body") or {}
    return bool(body.get("multipart") or body.get("raw"))


def raw_type(accepted: list[str], actual: str) -> str | None:
    """The Content-Type to send a file of type `actual` with, or None when the operation does
    not take it. `type/*` and `*/*` take the file's own type; `application/octet-stream` takes
    any file as bytes."""
    actual = actual.split(";", 1)[0].strip().lower()
    main = actual.split("/", 1)[0]
    if actual in accepted or f"{main}/*" in accepted or "*/*" in accepted:
        return actual
    if "application/octet-stream" in accepted:
        return "application/octet-stream"
    return None


def form_files(op: dict[str, Any], body: Any) -> list[tuple[str, str]]:
    """(field, upload_id) for every file a checked multipart body references; a whole-file body
    is the field `RAW_FIELD`."""
    spec = op["validate"].get("body") or {}
    if spec.get("raw") and uploads.is_ref(body):
        return [(RAW_FIELD, str(body["upload_id"]))]
    if not spec.get("multipart") or not isinstance(body, dict):
        return []
    out = []
    for name in spec["files"]:
        value = body.get(name)
        refs = value if isinstance(value, list) else [] if value is None else [value]
        out += [(name, str(ref["upload_id"])) for ref in refs]
    return out


def _form_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


async def _form(ctx: Context, p: Prepared) -> list[tuple[str, tuple[str | None, Any, str | None]]]:
    """Multipart parts: text fields first, then the stored files."""
    # 确认卡与审批执行的上下文不带部署配置（不转存结果），读上传的文件照样要用。
    settings = ctx.settings or get_settings()
    names = set(p.op["validate"]["body"]["files"])
    parts: list[tuple[str, tuple[str | None, Any, str | None]]] = [
        (name, (None, _form_text(value), None))
        for name, value in (p.body or {}).items()
        if name not in names and value is not None
    ]
    for name, row in p.files:
        data = await uploads.read(settings, row)
        parts.append((name, (row.filename, data, row.content_type)))
    return parts


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
            oauth_grant_id=ctx.member.oauth_grant_id,
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


# Not a refusal: the owner is absent and the write is staged for the owner's approval.
OWNER_APPROVAL = "owner_approval_required"
# A business system's task listed the writes it allows; this one is not among them.
CALLER_REFUSED = "write_not_allowed_by_caller"
# Recorded outcome of such a refusal (the task's `writes` shows it as `not_allowed`).
NOT_ALLOWED = "not_allowed"
# Recorded outcomes of a write a business system's silent task ran without a card, like
# `confirmed_` for cards: `silent_ok`, `silent_http_error`, `silent_unreachable`.
SILENT_PREFIX = "silent_"


def caller_refused(ctx: Context, system_key: str, operation_id: str, op: dict[str, Any]) -> bool:
    """A write the business system that started this task did not list in `allowed_writes`."""
    from coreman.core.api_tasks.service import write_ref

    return (
        op["risk"] in CONFIRMED_RISKS
        and ctx.caller_writes is not None
        and write_ref(system_key, operation_id) not in ctx.caller_writes
    )


# Recorded outcomes of a write run after the owner approved it, like `confirmed_` for cards.
APPROVED_PREFIX = "approved_"


def policy_error(ctx: Context, system: BusinessSystem, op: dict[str, Any]) -> str | None:
    """Why this operation may not be proxied for this AI employee, or None.

    `OWNER_APPROVAL` means it is not sent now but staged for the owner's approval: only a `write`,
    only in a personal assistant's unattended turn that may submit approvals, and only for systems
    whose grant allows writes. `destructive` stays refused while the owner is away.
    """
    if op["risk"] in CONFIRMED_RISKS and ctx.owner_absent:
        if op["risk"] == "write" and system.key in ctx.stageable:
            return OWNER_APPROVAL
        return "owner_turn_required"
    if op["risk"] in CONFIRMED_RISKS and system.key not in ctx.writable:
        return "write_not_allowed"
    if op["risk"] in ("destructive", *SINGLE_RISKS) and ctx.writes_preapproved:
        # 所有者批准的是一句任务描述，这一轮没有人逐项确认：删了恢复不了的、动钱的不在这里做。
        return "confirmation_required"
    return None


DENIALS = {
    "write_not_allowed": "这个 AI 员工没有该系统的写入权限。需要管理员在 AI 员工的「系统权限」里"
    "为这个系统开启「允许写入」；在此之前不要尝试其他写入方式。",
    "owner_turn_required": "这一轮是个人助理在所有者不在场时跑的，业务系统只能查询，不能做写操作；"
    "把要做的写操作写进给所有者的结论里，由所有者在私聊里亲自让助理去做。",
    "confirmation_required": "这一轮是所有者批准后由个人助理调度来的，没有人能逐项确认："
    "删除后无法恢复的操作（destructive）和涉及资金的操作（financial）不在这种轮次里执行。把要做的事写进答复里，"
    "由所有者在私聊里亲自让助理去做，在确认卡上确认。",
    "preapproved_writes_exhausted": "这一轮已经执行了 {limit} 个写操作，达到所有者一次批准"
    "能做的上限。停止写入，把已经完成和还没做的部分如实写进答复里；更多的修改请所有者"
    "在私聊里让助理去做（走确认卡，可以整批提交）。",
    CALLER_REFUSED: "发起这次任务的业务系统没有允许这个写操作，平台已拒绝，没有执行。不要换别的"
    "方式做同一件事；在回复里写明这一步没做、需要做什么。",
    OWNER_APPROVAL: "这一轮是个人助理在所有者不在场时跑的：写操作调用后不会当场执行，平台自动"
    "提交给所有者审批，批准后按原样执行并把结果交回本会话。不要重试，也不要换别的方式做同一件事。",
}


async def operation(
    ctx: Context, system_key: str, operation_id: str
) -> tuple[BusinessSystem, Any, dict[str, Any]] | dict[str, Any]:
    """(system, loaded catalog, operation) the member may call through the proxy; an error
    otherwise. Policy (risk, writes) is checked by the caller."""
    system = ctx.system(system_key)
    if system is None:
        return context.no_system(system_key)
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
    op = loaded.compiled["operations"].get(operation_id)
    codes = await context.held(ctx, system, loaded) if op is not None else None
    if op is None or not context.visible(op, codes):
        return context.operation_not_found(system, operation_id)
    if "validate" not in op:
        return context.unavailable(system)
    return system, loaded, op


async def denial(
    ctx: Context, system: BusinessSystem, operation_id: str, op: dict[str, Any]
) -> dict[str, Any] | None:
    """The refusal of a call policy does not allow (recorded), or None; `OWNER_APPROVAL` passes."""
    if caller_refused(ctx, system.key, operation_id, op):
        await record(ctx, system, operation_id, op, NOT_ALLOWED)
        return {
            "error": CALLER_REFUSED,
            "system": system.key,
            "operation_id": operation_id,
            "risk": op["risk"],
            "message": DENIALS[CALLER_REFUSED],
        }
    denied = policy_error(ctx, system, op)
    if denied is None or denied == OWNER_APPROVAL:
        return None
    await record(ctx, system, operation_id, op, "denied")
    return {
        "error": denied,
        "system": system.key,
        "operation_id": operation_id,
        "risk": op["risk"],
        "message": DENIALS[denied],
    }


async def prepare(ctx: Context, args: Call) -> Prepared | dict[str, Any]:
    """Every check a call passes before it may reach the business system; an error otherwise."""
    found = await operation(ctx, args.system, args.operation_id)
    if isinstance(found, dict):
        return found
    system, loaded, op = found
    refused = await denial(ctx, system, args.operation_id, op)
    if refused is not None:
        return refused
    return await build(ctx, system, loaded, op, args)


async def build(
    ctx: Context, system: BusinessSystem, loaded: Any, op: dict[str, Any], args: Call
) -> Prepared | dict[str, Any]:
    """The request of one call to an operation that passed `operation` and the policy."""
    defs = loaded.compiled.get("defs") or {}
    path, pairs, body, problems = build_request(op, defs, args)
    headers, header_problems = build_headers(op, defs, args)
    problems += header_problems
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
    files: list[tuple[str, StoredObject]] = []
    for name, upload_id in form_files(op, body):
        row = await uploads.resolve(ctx, upload_id)
        if row is None:
            return {
                "error": "upload_not_found",
                "field": name,
                "message": "文件不存在、已过期（1 小时）或不是本轮上传的。"
                "按 systems_describe 的说明重新上传后再调用。",
            }
        accepted = (op["validate"].get("body") or {}).get("raw")
        if accepted and raw_type(accepted, row.content_type) is None:
            return {
                "error": "invalid_arguments",
                "invalid": [f"body: file type {row.content_type} is not accepted"],
                "accepted": accepted,
                "message": "这个操作不接受这种文件类型。换成 accepted 里的类型后重新上传。",
            }
        files.append((name, row))
    return Prepared(
        system, args.operation_id, op, url, body, idempotency_headers(op), files, headers
    )


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
    headers = {**p.headers, "Accept": "application/json", **fetch.auth_headers(token)}
    value = key or str(uuid.uuid4())
    headers.update({name: value for name in p.keys})
    # With storage, a large body is read in full and kept as a file; otherwise only what fits.
    limit = STORE_MAX if ctx.settings is not None else MAX_RESPONSE_BYTES
    started = time.monotonic()
    with tempfile.SpooledTemporaryFile(max_size=MAX_RESPONSE_BYTES) as spool:
        try:
            timeout = READ_TIMEOUT if op["risk"] == "read" else CALL_TIMEOUT
            async with fetch.client_for(p.url, timeout=timeout) as client:
                body_spec = op["validate"].get("body") or {}
                if body_spec.get("multipart"):
                    sent: dict[str, Any] = {"files": await _form(ctx, p)}
                elif body_spec.get("raw") and p.files:
                    _, row = p.files[0]
                    data = await uploads.read(ctx.settings or get_settings(), row)
                    headers["Content-Type"] = raw_type(body_spec["raw"], row.content_type) or ""
                    sent = {"content": data}
                else:
                    sent = {"json": p.body} if p.body is not None else {}
                async with client.stream(op["method"], p.url, headers=headers, **sent) as response:
                    size = 0
                    cut_off = False
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > limit:
                            cut_off = True
                            break
                        await disk_io(partial(spool.write, part))
        except (fetch.FetchError, httpx.HTTPError, TimeoutError) as exc:
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
            # 连接都没建起来的，请求肯定没发出；其余的（等响应超时、连接中途断开）业务系统可能
            # 已经收到并处理了，不能当成「没做」。
            maybe_sent = not isinstance(exc, NOT_SENT)
            return {
                "error": "system_unreachable",
                "system": system.key,
                "maybe_sent": maybe_sent,
                "message": (
                    "请求已发出但没有等到业务系统的响应（超时或连接中断），业务系统可能已经处理。"
                    "不要重试：先用读操作核对实际结果，再告诉用户。"
                    if maybe_sent and op["risk"] != "read"
                    else "业务系统没有响应或连接失败。不要换别的方式重试同一个写操作，先告诉用户。"
                ),
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


async def _stage(ctx: Context, p: Prepared, args: Call) -> dict[str, Any]:
    """Unattended write: staged for the owner's approval, never sent now."""
    # 函数内导入：审批闸门依赖面广，目录代理平时用不到它。
    from coreman.core.assistants.approvals import proxied

    staged = await proxied.submit_system(
        ctx,
        p.system,
        p.op,
        operation_id=p.operation_id,
        path_params=args.path_params,
        query=args.query,
        headers=p.headers or None,
        body=p.body,
        url=str(p.url),
    )
    done = staged.get("status") == "pending_approval"
    await record(ctx, p.system, p.operation_id, p.op, "staged" if done else "denied")
    if staged.get("error") == "owner_turn_required":
        staged = {**staged, "message": DENIALS["owner_turn_required"]}
    return {"system": p.system.key, "operation_id": p.operation_id, **staged}


async def _preapproved_spent(ctx: Context) -> bool:
    """A turn whose writes the owner approved in advance runs them without a card, so it gets
    only as many as one card could hold: counted from the calls this task already made."""
    sent = await ctx.session.scalar(
        select(func.count())
        .select_from(SystemCall)
        .where(
            SystemCall.task_id == ctx.member.task_id,
            SystemCall.risk.in_(CONFIRMED_RISKS),
            SystemCall.outcome.in_(("ok", "http_error", "unreachable")),
        )
    )
    return int(sent or 0) >= PREAPPROVED_WRITES_MAX


async def call(ctx: Context, args: Call) -> dict[str, Any]:
    if ctx.own_agent:
        from coreman.core.systems_catalog import member_writes

        return await member_writes.call(ctx, args)
    if args.batch is not None:
        from coreman.core.systems_catalog import batch

        return await batch.submit(ctx, args)
    prepared = await prepare(ctx, args)
    if isinstance(prepared, dict):
        return prepared
    if policy_error(ctx, prepared.system, prepared.op) == OWNER_APPROVAL:
        if prepared.files:
            # 审批可能隔几小时才批，上传 1 小时就过期：带文件的写只在所有者在场时做。
            await record(ctx, prepared.system, prepared.operation_id, prepared.op, "denied")
            return {
                "error": "owner_turn_required",
                "system": prepared.system.key,
                "operation_id": prepared.operation_id,
                "message": DENIALS["owner_turn_required"],
            }
        return await _stage(ctx, prepared, args)
    if prepared.op["risk"] in CONFIRMED_RISKS and ctx.silent_writes:
        # 业务系统静默执行的任务：这一项在它的 `allowed_writes` 里（不在的已被 `denial` 拒绝），
        # 名单又是平台管理员给这个调用方登记过的。人工复核在业务系统里做，这里不发确认卡，
        # 以员工身份当场执行，记录带 `silent_` 前缀。
        return await send(ctx, prepared, prefix=SILENT_PREFIX)
    if prepared.op["risk"] in CONFIRMED_RISKS and not ctx.writes_preapproved:
        from coreman.core.systems_catalog import confirm

        return await confirm.propose(ctx, prepared, args)
    if prepared.op["risk"] in CONFIRMED_RISKS and await _preapproved_spent(ctx):
        await record(ctx, prepared.system, prepared.operation_id, prepared.op, "denied")
        return {
            "error": "preapproved_writes_exhausted",
            "stop": True,
            "system": prepared.system.key,
            "operation_id": prepared.operation_id,
            "message": DENIALS["preapproved_writes_exhausted"].format(limit=PREAPPROVED_WRITES_MAX),
        }
    return await send(ctx, prepared)
