"""Batch writes through the platform proxy: many items of one operation, one confirmation.

`systems_call` with `batch` reads an uploaded JSON Lines file (one item per line), checks every
line like a single call and stages them all, or none when any line fails. When the turn ends the
asker gets one card: the total, per-field value counts, a sample of at most `PREVIEW_MAX` items and
a link to every item. Once confirmed the platform runs the items one by one in background tasks of
`RUN_KIND`, carrying on after a failed item, shows the progress on the card (with a stop button)
and finally the outcome, then hands the result back to the conversation. An item is marked
`sending` before it goes out; one still `sending` when a run resumes is `unknown` and never sent
again.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import random
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BusinessSystem,
    InboundEvent,
    InteractionState,
    SystemBatchItem,
    Task,
)
from coreman.core.logging import get_logger
from coreman.core.object_store import object_store
from coreman.core.systems_catalog import confirm, policy, proxy, uploads
from coreman.core.systems_catalog.context import Context, carried
from coreman.core.timeutils import aware_utc, utcnow

log = get_logger(__name__)

KIND = "systems_batch"
CARD_EVENT = "systems_batch"
RUN_KIND = "systems_batch_run"
AAD = "systems_batch.v1"
PREVIEW_MAX = 50
# The batch file: checked, sealed and turned into CSV in memory. As large as any upload, so the
# largest batch a system allows (`PROXY_LIMITS`) fits at a few hundred bytes a line.
MAX_FILE_BYTES = uploads.UPLOAD_LIMIT
# Staged items are inserted this many rows a statement.
INSERT_ROWS = 1000
# How a run waits out the system's interval between items (replaced in tests).
_sleep = asyncio.sleep
# Fields the card counts values for; the rest are only in the detail file.
STATS_FIELDS_MAX = 20
STATS_VALUE_MAX = 40
ERRORS_SHOWN = 20
TOP_VALUES = 3
REPEATS_SHOWN = 5
CHANGES_SHOWN = 3
FAILURES_ON_CARD = 10
FAILURES_TO_MODEL = 20
BRIEF_MAX = 300
# One run task handles this many items or this many seconds, then queues the next one.
CHUNK_ITEMS = 500
CHUNK_SECONDS = 60.0
# The progress card is updated at most every `PROGRESS_MIN_SECONDS`, and then only once the batch
# moved on by `PROGRESS_STEP` of its items or `PROGRESS_MAX_SECONDS` have passed: a small batch
# shows progress every few seconds, a large one about once a minute, never more than about a
# hundred updates plus one a minute.
PROGRESS_MIN_SECONDS = 10.0
PROGRESS_MAX_SECONDS = 60.0
PROGRESS_STEP = 0.01
# An item marked `sending` this recently may still be in flight (its run task was taken for lost
# but is alive): the next stretch waits this long before calling it unknown. A call may wait up to
# `proxy.CALL_TIMEOUT` for each read of the response.
SEND_GRACE = 120.0
DONE, MORE, WAIT, SUPERSEDED, CANCELLED = "done", "more", "wait", "superseded", "cancelled"
# A batch whose run task keeps getting lost without any item done in between is queued again this
# many times, at least `REVIVE_GAP` seconds apart, then finished with the rest not run.
MAX_REVIVES = 3
REVIVE_GAP = 60.0
STOP = "stop"
# Card verbs: carry on after a pause; run the items not run yet, or the failed ones, again.
GO, CONTINUE, RETRY = "go", "continue", "retry"
# A batch of more than `TRIAL_OVER` items first runs `TRIAL_ITEMS` and pauses for the asker to
# check them in the business system.
TRIAL_ITEMS = 5
TRIAL_OVER = 10
# This many items in a row without success pause the batch: the business system is probably down
# or refusing everything, and the asker decides whether to carry on.
FAIL_STREAK = 10
# A paused batch nobody answers is finished after this long, the rest not run.
PAUSE_TTL = confirm.TTL
PAUSE_TRIAL, PAUSE_FAILURES = "pause_trial", "pause_failures"
FAILED = ("http_error", "unreachable")
NOT_OK = (*FAILED, "unknown")
LABELS = {
    "ok": "成功",
    "http_error": "失败",
    "unreachable": "没连上",
    "unknown": "结果未知",
    "skipped": "未执行",
    "pending": "未执行",
    "sending": "结果未知",
}
HINT = (
    "已整批暂存，没有执行。本轮结束时平台给提问人发一张确认卡（总数、字段统计、抽样预览和完整"
    "明细下载），点确认后平台逐项执行；超过 10 项时先执行前 5 项并暂停，等提问人核对后继续。"
    "告诉用户需要在卡片上确认，不要说已经完成。"
)

Scalar = proxy.Scalar


class Row(BaseModel):
    """One line of the batch file."""

    # 与 proxy.Call 一样去掉首尾空白：暂存、判重、展示用的就是发出去的值。
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    path_params: dict[str, Scalar] | None = None
    query: dict[str, Scalar | list[Scalar]] | None = None
    body: Any = None
    target: str | None = Field(default=None, max_length=200)
    before: dict[str, Any] | None = None


def _aad(batch_id: uuid.UUID, seq: int | None = None) -> str:
    return f"{AAD}:{batch_id}" + ("" if seq is None else f":{seq}")


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _dump(value: Any) -> str:
    """JSON that keeps the order fields were given in (shown on the card as given)."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, **extra, "message": message}


# ---------------------------------------------------------------- submit


def max_items(system: BusinessSystem, risk: str) -> int:
    """How many items one batch of an operation of `risk` may hold on this system."""
    return {"write": system.batch_write_max, "destructive": system.batch_destructive_max}.get(
        risk, 0
    )


def _no_constant(name: str) -> Any:
    """NaN and Infinity are not JSON and cannot be sent as a request body."""
    raise ValueError(name)


def parse(data: bytes, cap: int) -> tuple[list[tuple[int, Row]], list[str], int]:
    """(line number, row) of every non-blank line, the problems found and the item count."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], ["文件不是 UTF-8 编码的 JSON Lines"], 0
    # 只按换行切：U+2028 之类在 JSON 字符串里是合法字符，不能当成行尾。
    lines = [(n, line) for n, line in enumerate(text.split("\n"), 1) if line.strip()]
    if not lines:
        return [], ["文件里没有任何一行数据"], 0
    if len(lines) > cap:
        return [], [f"共 {len(lines)} 项，超过这个操作一批最多 {cap} 项的上限"], len(lines)
    rows: list[tuple[int, Row]] = []
    problems: list[str] = []
    for number, line in lines:
        try:
            raw = json.loads(line, parse_constant=_no_constant)
        except ValueError:
            problems.append(f"第 {number} 行：不是合法的 JSON（NaN、Infinity 也不行）")
            continue
        if not isinstance(raw, dict):
            problems.append(f"第 {number} 行：每行应是一个 JSON 对象")
            continue
        try:
            rows.append((number, Row.model_validate(raw)))
        except ValidationError as exc:
            first = exc.errors(include_url=False, include_input=False)[0]
            where = ".".join(str(part) for part in first["loc"])
            problems.append(f"第 {number} 行：{where}: {first['msg']}")
    return rows, problems, len(lines)


def _request(row: Row) -> dict[str, Any]:
    out = {
        "path_params": row.path_params,
        "query": row.query,
        "body": row.body,
        "target": row.target,
        "before": row.before,
    }
    return {k: v for k, v in out.items() if v is not None}


def _fields(item: dict[str, Any]) -> dict[str, Any]:
    """The values the card counts: the request body's top-level fields, else the query."""
    body = item.get("body")
    if isinstance(body, dict):
        return body
    query = item.get("query")
    return query if isinstance(query, dict) and body is None else {}


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Per field: the most frequent values and how many items carry them; objects repeated."""
    counts: dict[str, Counter[str]] = {}
    for item in items:
        for key, value in _fields(item).items():
            counts.setdefault(key, Counter())[_canon(value)] += 1
    fields = []
    for key, counter in counts.items():
        top = counter.most_common(TOP_VALUES)
        rest = sum(counter.values()) - sum(n for _, n in top)
        fields.append(
            {
                "field": key,
                "top": [[json.loads(value), n] for value, n in top],
                "other_values": max(0, len(counter) - len(top)),
                "other_items": rest,
                "missing": len(items) - sum(counter.values()),
            }
        )
    objects = Counter(_canon(item["path_params"]) for item in items if item.get("path_params"))
    repeats = [[json.loads(k), n] for k, n in objects.most_common() if n > 1]
    return {"fields": fields, "repeats": repeats[:REPEATS_SHOWN], "repeat_count": len(repeats)}


def pick(items: list[dict[str, Any]], seed: int) -> list[int]:
    """Row indexes for the card's sample, at most `PREVIEW_MAX`: the rare values of each field
    first, taking turns between fields (skipping fields whose values are mostly different, like a
    name or an ID), then items whose object repeats, then a seeded random fill."""
    chosen: list[int] = []
    taken: set[int] = set()

    def take(index: int) -> None:
        if index not in taken and len(chosen) < PREVIEW_MAX:
            taken.add(index)
            chosen.append(index)

    first_with: dict[str, dict[str, int]] = {}
    counts: dict[str, Counter[str]] = {}
    for index, item in enumerate(items):
        for key, value in _fields(item).items():
            value_key = _canon(value)
            counts.setdefault(key, Counter())[value_key] += 1
            first_with.setdefault(key, {}).setdefault(value_key, index)
    queues = []
    for key, counter in counts.items():
        # 只有一种取值的字段没什么可挑；大半取值都不同的（名称、编号）按值挑也看不出异常。
        if not 1 < len(counter) <= sum(counter.values()) // 2:
            continue
        firsts = first_with[key]
        rare_first = sorted(counter, key=lambda v: (counter[v], firsts[v]))
        queues.append([firsts[value] for value in rare_first])
    for turn in range(max((len(q) for q in queues), default=0)):
        for queue in queues:
            if turn < len(queue):
                take(queue[turn])
    objects = Counter(_canon(item["path_params"]) for item in items if item.get("path_params"))
    for index, item in enumerate(items):
        if item.get("path_params") and objects[_canon(item["path_params"])] > 1:
            take(index)
    rest = [index for index in range(len(items)) if index not in taken]
    random.Random(seed).shuffle(rest)
    for index in rest:
        take(index)
    return sorted(chosen)


async def submit(ctx: Context, args: proxy.Call) -> dict[str, Any]:
    """Check and stage a whole batch for the asker's confirmation."""
    assert args.batch is not None
    if args.path_params is not None or args.query is not None or args.body is not None:
        return _error(
            "invalid_tool_or_arguments",
            "batch 与 path_params、query、body 不能同时传：每一项的参数写在上传的文件里。",
        )
    found = await proxy.operation(ctx, args.system, args.operation_id)
    if isinstance(found, dict):
        return found
    system, loaded, op = found
    refused = await proxy.denial(ctx, system, args.operation_id, op)
    if refused is not None:
        return refused
    if op["risk"] not in proxy.CONFIRMED_RISKS:
        return _error("batch_read_only", "batch 只用于写操作；读操作直接逐次调用 systems_call。")
    if (op["validate"].get("body") or {}).get("multipart"):
        return _error("batch_files_unsupported", "带文件的操作不能批量提交，请逐项调用。")
    session, member = ctx.session, ctx.member
    bot = await session.get(Bot, member.bot_id)
    if bot is None or bot.kind == "assistant":
        # 助理的内容按所有者的数据密钥封存；逐项暂存的表不在那一层里，先不开放。
        return _error("batch_unavailable", "个人助理不支持批量提交，请逐项调用 systems_call。")
    unavailable = _error(
        "confirmation_unavailable",
        "批量写操作只能在提问人本人发来消息、能当场点确认卡的回合里提交。" + confirm.UNAVAILABLE,
    )
    if ctx.writes_preapproved or proxy.policy_error(ctx, system, op) == proxy.OWNER_APPROVAL:
        await proxy.record(ctx, system, args.operation_id, op, "denied")
        return unavailable
    assert member.task_id is not None
    origin = await confirm.confirmable_origin(session, member.task_id, member.user.id)
    if origin is None:
        await session.commit()
        return unavailable
    if await confirm._batch(session, member.bot_id, member.task_id, kind=KIND) is not None:
        await session.commit()
        return _error(
            "batch_exists",
            "本轮已经提交过一批，一轮只能提交一批。等用户确认后再让用户另发一条消息提交下一批。",
        )
    upload = await uploads.resolve(ctx, args.batch.upload_id)
    if upload is None:
        await session.commit()
        return _error(
            "upload_not_found",
            "批量文件不存在、已过期（1 小时）或不是本轮上传的。"
            "按 systems_describe 的说明重新上传。",
        )
    if upload.size > MAX_FILE_BYTES:
        await session.commit()
        return _error(
            "batch_too_large",
            f"批量文件不能超过 {MAX_FILE_BYTES // (1024 * 1024)} MB。每行只写这一项要改的参数，"
            "或分成几批、让用户分几条消息提交。",
        )
    data = await uploads.read(ctx.settings or get_settings(), upload)
    cap = max_items(system, op["risk"])
    rows, problems, total = parse(data, cap)
    items: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for number, row in rows:
        call = proxy.Call(
            system=args.system,
            operation_id=args.operation_id,
            path_params=row.path_params,
            query=row.query,
            body=row.body,
        )
        built = await proxy.build(ctx, system, loaded, op, call)
        if isinstance(built, dict):
            if built.get("error") != "invalid_arguments":
                await session.commit()
                return built
            problems.append(f"第 {number} 行：" + "；".join(built["invalid"][:3]))
            continue
        # 按实际发出的请求判重，不按文件里的写法。
        key = _canon([str(built.url), built.body])
        if key in seen:
            problems.append(f"第 {number} 行：与第 {seen[key]} 行完全相同")
            continue
        seen[key] = number
        items.append(_request(row))
    if problems:
        await session.commit()
        return _error(
            "invalid_batch",
            "整批没有暂存。按行号修正文件后重新上传、重新提交整批。",
            items=total,
            problem_count=len(problems),
            problems=problems[:ERRORS_SHOWN],
        )
    chat_id, chat_type = origin
    now = utcnow()
    note = (
        args.note.model_dump(exclude_none=True, include={"impact", "rollback"}) if args.note else {}
    )
    sealed = {
        "note": note,
        "summary": summarize(items),
        "preview": [[index + 1, items[index]] for index in pick(items, member.task_id)],
    }
    meta = {
        "origin_task_id": member.task_id,
        "user_id": str(member.user.id),
        "open_id": await _asker_open_id(session, member.task_id),
        "chat_id": chat_id,
        "chat_type": chat_type,
        "system": system.key,
        "system_name": system.name,
        "operation_id": args.operation_id,
        "summary": op.get("summary") or "",
        "method": op["method"],
        "path": op["path"],
        "server": _server(system, loaded),
        "risk": op["risk"],
        "op_digest": confirm.op_digest(op),
        "count": len(items),
    }
    # 卡片放不下就当场说，不等飞书拒收之后才让用户知道。
    if not fits(meta, sealed):
        await session.commit()
        return _error(
            "batch_card_too_large",
            "这一批的内容放不进一张确认卡（字段太多或取值太长）。每行只写这一项要改的字段，"
            "或者请用户在业务系统里用批量导入完成。",
        )
    state = InteractionState(
        bot_id=member.bot_id,
        kind=KIND,
        scope_key=str(member.task_id),
        status="open",
        # Sent cards get their own deadline; an unsent batch outlives its turn only briefly.
        expires_at=now + timedelta(seconds=ctx.token_ttl) + confirm.TTL,
        state={},
    )
    session.add(state)
    await session.flush()
    cipher = ctx.issuance.cipher
    # 每项按批次和行号加密：密文换了行或换了批都解不开，执行时不会串。
    for first in range(0, len(items), INSERT_ROWS):
        await session.execute(
            insert(SystemBatchItem),
            [
                {
                    "batch_id": state.id,
                    "seq": seq,
                    "request_enc": cipher.encrypt(_dump(item), _aad(state.id, seq)),
                    "idempotency_key": str(uuid.uuid4()),
                }
                for seq, item in enumerate(items[first : first + INSERT_ROWS], first + 1)
            ],
        )
    state.state = {**meta, "enc": cipher.encrypt(_dump(sealed), _aad(state.id))}
    await proxy.record(ctx, system, args.operation_id, op, "batch_proposed")
    return {"status": "pending_confirmation", "count": len(items), "hint": HINT}


async def _asker_open_id(session: AsyncSession, task_id: int) -> str:
    """The asker's Feishu open_id (for mentions), from the message that started the turn."""
    task = await session.get(Task, task_id)
    event = (
        await session.get(InboundEvent, task.inbound_event_id)
        if task is not None and task.inbound_event_id is not None
        else None
    )
    return str(event.sender_open_id or "") if event is not None else ""


def _server(system: Any, loaded: Any) -> str:
    """Where the operation's path is appended: part of what the asker confirms."""
    return str(loaded.compiled.get("server") or system.base_url or "")


def instructions(public_base_url: str, system: BusinessSystem, risk: str) -> dict[str, Any]:
    """How to submit many items of a write operation at once (part of systems_describe)."""
    url = public_base_url.rstrip("/") + uploads.UPLOAD_PATH + "?filename=batch.jsonl"
    return {
        "max_items": max_items(system, risk),
        "file": "JSON Lines，UTF-8，每行一项："
        '{"path_params": {...}, "query": {...}, "body": {...}, "target": "对象的业务名称", '
        '"before": {"字段": "修改前的值"}}；path_params、query、body 与单次调用相同，'
        "target、before 选填，只用于确认卡展示。"
        f"文件不超过 {MAX_FILE_BYTES // (1024 * 1024)} MB，不能有 NaN、Infinity。",
        "upload": f'curl -sS -X PUT -H "Authorization: Bearer ${policy.TOKEN_ENV}" '
        f"--data-binary @<本地文件路径> '{url}'",
        "call": 'systems_call(system, operation_id, batch={"upload_id": "..."}, '
        'note={"impact": "...", "rollback": "..."})',
        "notes": "同一个写操作要做多项时，用脚本把数据写成文件、上传后整批提交一次，不要逐项调用。"
        "有任何一行不合格整批不暂存，按返回的行号修正后重新提交。",
    }


# ---------------------------------------------------------------- card


def _sealed(cipher: Cipher, state: InteractionState) -> dict[str, Any]:
    data = json.loads(cipher.decrypt(str(state.state["enc"]), _aad(state.id)))
    assert isinstance(data, dict)
    return data


def _value(value: Any) -> str:
    return confirm._plain(value)


def _object(item: dict[str, Any]) -> str:
    if item.get("target"):
        return _value(item["target"])
    params = item.get("path_params") or {}
    return "，".join(f"{_value(k)}={_value(v)}" for k, v in params.items())


def _params(item: dict[str, Any]) -> str:
    """The real request parameters of an item: path parameters, and the query when the counted
    fields are the body's."""
    pairs = dict(item.get("path_params") or {})
    if isinstance(item.get("body"), dict):
        pairs.update(item.get("query") or {})
    return "，".join(f"{confirm._plain(k, 30)}={_value(v)}" for k, v in pairs.items())


def _preview_object(item: dict[str, Any]) -> str:
    """What a preview line names: the AI's name for the object is never shown without the real
    parameters beside it, so a name matched to the wrong row can be seen on the card."""
    params = _params(item)
    if item.get("target"):
        return _value(item["target"]) + (f"［{params}］" if params else "")
    return params


def _named(item: dict[str, Any]) -> str:
    name = _object(item)
    return f"（{name}）" if name else ""


def _changes(item: dict[str, Any]) -> str:
    before = item.get("before") or {}
    parts = []
    for key, after in list(_fields(item).items())[:CHANGES_SHOWN]:
        field = confirm._plain(key, 30)
        if key in before:
            parts.append(f"{field}：{_value(before[key])} → {_value(after)}")
        else:
            parts.append(f"{field}：{_value(after)}")
    more = len(_fields(item)) - CHANGES_SHOWN
    return "；".join(parts) + (f"；另有 {more} 个字段" if more > 0 else "")


def stats_lines(summary: dict[str, Any]) -> list[str]:
    lines = []
    for field in summary["fields"][:STATS_FIELDS_MAX]:
        values = [
            f"{confirm._plain(value, STATS_VALUE_MAX)}（{n} 项）" for value, n in field["top"]
        ]
        if field["other_values"]:
            values.append(f"其他 {field['other_values']} 种取值（{field['other_items']} 项）")
        if field["missing"]:
            values.append(f"未填（{field['missing']} 项）")
        lines.append(f"{confirm._plain(field['field'], 30)}：" + "、".join(values))
    hidden = len(summary["fields"]) - STATS_FIELDS_MAX
    if hidden > 0:
        lines.append(f"…另有 {hidden} 个字段没有列出，见明细")
    if summary["repeats"]:
        shown = "、".join(
            f"{_object({'path_params': obj})}（{n} 次）" for obj, n in summary["repeats"]
        )
        more = summary["repeat_count"] - len(summary["repeats"])
        lines.append(
            "⚠️ 同一对象出现多次："
            + shown
            + (f" 等 {summary['repeat_count']} 个" if more > 0 else "")
        )
    return lines


def _button(origin: int, state_id: uuid.UUID, label: str, verb: str, kind: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "task_id": f"personal:{origin}",
                    "event_key": CARD_EVENT,
                    "level": f"{verb}:{state_id}",
                },
            }
        ],
    }


def _link(url: str | None, label: str) -> list[dict[str, Any]]:
    if not url:
        return []
    return [{"tag": "markdown", "content": f"[{label}]({url})"}]


def _title(meta: dict[str, Any]) -> str:
    return confirm.title({"summary": meta.get("summary"), "operation_id": meta["operation_id"]})


def _card_body(
    meta: dict[str, Any],
    state_id: uuid.UUID,
    sealed: dict[str, Any],
    preview: list[Any],
    url: str | None,
) -> dict[str, Any]:
    origin = int(meta["origin_task_id"])
    count = int(meta["count"])
    destructive = meta["risk"] == "destructive"
    intro = f"共 {count} 项。以下按字段统计"
    intro += f"，并抽样预览 {len(preview)} 项" if len(preview) < count else "，并列出全部各项"
    intro += "，完整明细见下载。" if url else "。"
    if count > TRIAL_OVER:
        intro += f"\n确认后先执行前 {TRIAL_ITEMS} 项并暂停，你核对无误后再继续其余各项。"
    if destructive:
        intro += "\n⚠️ 删除后无法恢复。"
    lines = [
        intro,
        "",
        "【字段统计】",
        *(stats_lines(sealed["summary"]) or ["（没有可统计的字段）"]),
    ]
    lines += ["", f"【预览 {len(preview)} / {count}】"]
    for seq, item in preview:
        changes = _changes(item)
        named = _preview_object(item)
        lines.append(f"{seq}. " + "  ".join(part for part in (named, changes) if part))
    detail = [f"{meta['system_name']} · {meta['method']} {meta['path']}"]
    note = sealed.get("note") or {}
    if note.get("impact"):
        detail.append(f"影响（AI 说明）：{confirm._value(note['impact'])}")
    if note.get("rollback"):
        detail.append(f"恢复方法（AI 说明）：{confirm._value(note['rollback'])}")
    return {
        "schema": "2.0",
        "task_id": f"personal:{origin}",
        "header": {
            "template": "red" if destructive else "orange",
            "title": {"tag": "plain_text", "content": f"请确认 {count} 项操作"},
            "subtitle": {
                "tag": "plain_text",
                "content": confirm._plain(f"{meta['system_name']} · {_title(meta)}"),
            },
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)}},
                *_link(url, "下载全部明细（CSV）"),
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {"title": {"tag": "markdown", "content": confirm.DETAIL_TITLE}},
                    "background_color": "grey",
                    "border": {"color": "grey", "corner_radius": "8px"},
                    "padding": "8px",
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": "\n".join(detail),
                                "text_size": "notation",
                            },
                        }
                    ],
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                _button(
                                    origin,
                                    state_id,
                                    f"确认执行 {count} 项",
                                    "confirm",
                                    "danger" if destructive else "primary",
                                )
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [_button(origin, state_id, "取消", "cancel", "default")],
                        },
                    ],
                },
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>对象与修改前的值由 AI 整理，以明细中的请求为准。"
                        "平台逐项执行，某一项失败会继续执行其余各项。"
                        "只有发起人能确认，60 分钟内有效。</font>"
                    ),
                    "text_size": "notation",
                },
            ]
        },
    }


def render(
    meta: dict[str, Any], state_id: uuid.UUID, sealed: dict[str, Any], url: str | None
) -> dict[str, Any]:
    """The confirmation card, with fewer sample items until it fits."""
    preview = list(sealed["preview"])
    while True:
        body = _card_body(meta, state_id, sealed, preview, url)
        if confirm._bytes(body) <= confirm.CARD_BYTES_MAX or not preview:
            return body
        preview = preview[: len(preview) // 2]


def fits(meta: dict[str, Any], sealed: dict[str, Any]) -> bool:
    """Whether the batch can be shown on one card at all (with a link as long as a real one)."""
    body = render(meta, uuid.UUID(int=0), sealed, "https://example.com/" + "x" * 400)
    return confirm._bytes(body) <= confirm.CARD_BYTES_MAX


def _cell(value: Any) -> str:
    text = value if isinstance(value, str) else "" if value is None else _canon(value)
    # 表格软件把 = + - @ 开头的单元格当公式：前面加单引号，打开时只显示文字。
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


async def _rows(
    session: AsyncSession, cipher: Cipher, batch_id: uuid.UUID
) -> list[tuple[SystemBatchItem, dict[str, Any]]]:
    items = await session.scalars(
        select(SystemBatchItem)
        .where(SystemBatchItem.batch_id == batch_id)
        .order_by(SystemBatchItem.seq)
        # 状态以库里为准，不用本会话里可能过时的对象。
        .execution_options(populate_existing=True)
    )
    return [
        (item, json.loads(cipher.decrypt(item.request_enc, _aad(batch_id, item.seq))))
        for item in items
    ]


async def _csv_link(
    session: AsyncSession,
    filename: str,
    header: list[str],
    lines: list[list[Any]],
    *,
    expires_at: datetime | None,
) -> str | None:
    """Store a CSV in short-term storage and return its download link; None when unavailable."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows([[_cell(v) for v in line] for line in lines])
    data = ("﻿" + out.getvalue()).encode()

    async def source() -> AsyncIterator[bytes]:
        yield data

    settings = get_settings()
    now = utcnow()
    try:
        row = await object_store(settings).put(
            session, source(), filename=filename, content_type="text/csv", now=now
        )
    except Exception as exc:  # noqa: BLE001 存储不可用时卡片不带下载链接
        log.warning("systems_batch_csv_failed", error_type=type(exc).__name__)
        return None
    if expires_at is not None:
        row.expires_at = expires_at
    await session.flush()
    return object_store(settings, row.backend).url(row)


DETAIL_HEADER = ["序号", "对象", "路径参数", "查询参数", "请求体", "修改前（AI 提供）"]


def _detail(seq: int, item: dict[str, Any]) -> list[Any]:
    return [
        seq,
        item.get("target"),
        item.get("path_params"),
        item.get("query"),
        item.get("body"),
        item.get("before"),
    ]


async def card(
    session: AsyncSession, cipher: Cipher, state: InteractionState
) -> dict[str, Any] | None:
    """The confirmation card with a fresh link to every staged item (valid as long as the card).
    None when the detail file could not be stored: a card showing only a sample of what it
    confirms is not sent."""
    rows = await _rows(session, cipher, state.id)
    url = await _csv_link(
        session,
        f"{state.state['operation_id']}-明细.csv",
        DETAIL_HEADER,
        [_detail(item.seq, request) for item, request in rows],
        expires_at=utcnow() + confirm.TTL,
    )
    if url is None:
        return None
    return render(state.state, state.id, _sealed(cipher, state), url)


NO_DETAIL = (
    "这批 {count} 项写操作的明细文件没能生成，确认卡没有发出，这一批不会执行。"
    "请稍后再发一条消息，让 AI 员工重新提交。"
)


async def release(
    session: AsyncSession, cipher: Cipher, *, bot_id: uuid.UUID, task_id: int, send: bool
) -> bool:
    """At the end of a turn: send the card of its batch, or drop it when the turn did not finish
    normally. True when a card was queued."""
    state = await confirm._batch(session, bot_id, task_id, kind=KIND)
    if state is None or state.status != "open" or state.state.get("sent"):
        return False
    if not send:
        state.status = "cancelled"
        return False
    body = await card(session, cipher, state)
    if body is None:
        state.status = "cancelled"
        await outbox.add(
            session,
            bot_id=state.bot_id,
            platform="feishu",
            kind="send",
            dedupe_key=f"{confirm.card_key(KIND, state.id, 0)}:no-detail",
            target={"chat_id": state.state["chat_id"]},
            payload={
                "card": confirm.notice_card(
                    "写操作没有执行", NO_DETAIL.format(count=state.state["count"])
                )
            },
        )
        return False
    await confirm.send_card(session, state, body, now=utcnow())
    return True


# ---------------------------------------------------------------- run


async def start(session: AsyncSession, state: InteractionState) -> None:
    """Confirmed: run it in the background, from the first item."""
    state.status = "submitted"
    # 执行中不让清理任务碰它；跑完再给保留期。
    state.expires_at = None
    now = utcnow().isoformat()
    # 确认后立刻显示 0 进度：从这一刻起按 progress_due 的节奏更新。
    meta = {
        **state.state,
        "started_at": now,
        "started_done": 0,
        "chunk": 1,
        "progress_at": now,
        "progress_done": 0,
    }
    if int(meta["count"]) > TRIAL_OVER:
        meta["trial"] = TRIAL_ITEMS
    state.state = meta
    await _enqueue(session, state, 1)


async def _enqueue(
    session: AsyncSession, state: InteractionState, chunk: int, *, delay: float = 0.0
) -> None:
    await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=state.bot_id,
            kind=RUN_KIND,
            dedupe_key=f"systems-batch:{state.id}:run:{chunk}",
            payload={"batch_id": str(state.id), "chunk": chunk},
            run_after=utcnow() + timedelta(seconds=delay) if delay > 0 else None,
        ),
    )


async def interval(session: AsyncSession, state: InteractionState) -> float:
    """Seconds to wait before each item of this batch: its system's current setting."""
    system = await session.get(BusinessSystem, state.state["system"])
    return (system.batch_interval_ms if system is not None else 0) / 1000


async def counts(session: AsyncSession, batch_id: uuid.UUID) -> Counter[str]:
    rows = await session.execute(
        select(SystemBatchItem.status, func.count())
        .where(SystemBatchItem.batch_id == batch_id)
        .group_by(SystemBatchItem.status)
    )
    return Counter({status: int(n) for status, n in rows.all()})


def tally(found: Counter[str]) -> str:
    failed = found["http_error"] + found["unreachable"]
    parts = [f"成功 {found['ok']}", f"失败 {failed}"]
    if found["unknown"]:
        parts.append(f"结果未知 {found['unknown']}")
    if found["skipped"]:
        parts.append(f"未执行 {found['skipped']}")
    return "，".join(parts)


BAR_CELLS = 20


def bar(done: int, total: int) -> str:
    """A text progress bar: plain text renders the same on every client and theme."""
    share = done / total if total else 1.0
    filled = min(BAR_CELLS, int(share * BAR_CELLS))
    return "█" * filled + "░" * (BAR_CELLS - filled) + f" {int(share * 100)}%"


def remaining(meta: dict[str, Any], done: int, total: int, now: datetime) -> str:
    """A rough estimate of the time left from the pace so far; empty before anything is done."""
    try:
        started = aware_utc(datetime.fromisoformat(str(meta.get("started_at"))))
    except ValueError:
        return ""
    elapsed = (now - started).total_seconds()
    # 暂停、收尾后再继续时重新计速：停着的那段时间不算。
    paced = done - int(meta.get("started_done") or 0)
    if paced <= 0 or done >= total or elapsed <= 0:
        return ""
    seconds = elapsed / paced * (total - done)
    if seconds < 60:
        return "预计不到 1 分钟完成。"
    return f"预计还需约 {round(seconds / 60)} 分钟。"


def progress_card(state: InteractionState, found: Counter[str]) -> dict[str, Any]:
    meta = state.state
    total = int(meta["count"])
    done = total - found["pending"] - found["sending"]
    stopping = bool(meta.get(STOP))
    text = f"{bar(done, total)}\n已处理 {done} / {total}：{tally(found)}。"
    left = remaining(meta, done, total, utcnow())
    if left and not stopping:
        text += f"\n{left}"
    text += "\n已请求停止，正在处理的这一项完成后停下。" if stopping else ""
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": text}}
    ]
    if not stopping:
        elements.append(_button(int(meta["origin_task_id"]), state.id, "停止执行", STOP, "default"))
    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"正在执行 {total} 项操作"},
            "subtitle": {
                "tag": "plain_text",
                "content": confirm._plain(f"{meta['system_name']} · {_title(meta)}"),
            },
        },
        "body": {"elements": elements},
    }


def _brief(result: dict[str, Any]) -> str:
    data = {k: v for k, v in result.items() if k not in ("system", "operation_id", "content_trust")}
    text = _canon(data)
    return text[:BRIEF_MAX] + ("…" if len(text) > BRIEF_MAX else "")


class ItemCrashed(Exception):
    """Sending one item raised something unexpected. Whether the request left is not known; the
    caller rolls back, records the item as unknown (`mark_unknown`) and carries on with a fresh
    task, since the session and everything loaded in it can no longer be trusted."""

    def __init__(self, seq: int, kind: str) -> None:
        super().__init__(kind)
        self.seq = seq
        self.kind = kind


class Halt(Exception):
    """Something every remaining item depends on is gone: stop, the rest stays unsent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def checked(pctx: Context, state: InteractionState) -> tuple[Any, Any, dict[str, Any]]:
    """The operation as confirmed, re-read against today's grants and catalog; `Halt` otherwise."""
    meta = state.state
    found = await proxy.operation(pctx, meta["system"], meta["operation_id"])
    if isinstance(found, dict):
        raise Halt(str(found.get("message") or found.get("error")))
    system, loaded, op = found
    denied = proxy.policy_error(pctx, system, op)
    if denied is not None:
        raise Halt(proxy.DENIALS.get(denied, denied))
    if op["risk"] != meta["risk"] or confirm.op_digest(op) != meta["op_digest"]:
        raise Halt("业务系统的操作定义已经变化")
    # 确认的是「这个地址下的这个操作」：目录里的服务器前缀变了，请求就会发到别处。
    if "server" in meta and _server(system, loaded) != meta["server"]:
        raise Halt("业务系统的接口地址已经变化")
    return system, loaded, op


def _current(state: InteractionState | None, chunk: int, *, paused_too: bool = False) -> bool:
    """This run task is still the one the batch expects: nobody queued a later stretch in its
    place (`revive`, after the task was taken for lost), the batch has not been finished and,
    unless `paused_too` (finishing a paused batch), it is not waiting for the asker."""
    return (
        state is not None
        and state.status == "submitted"
        and not state.state.get("finished_at")
        and (paused_too or not state.state.get("paused"))
        and int(state.state.get("chunk") or 0) == chunk
    )


async def run_chunk(
    pctx: Context,
    cipher: Cipher,
    state: InteractionState,
    *,
    chunk: int,
    clock: Callable[[], float],
    cancelled: Callable[[], bool] = lambda: False,
) -> str:
    """Run the next items in order, as the asker, in `pctx.session` (which holds `state`).

    `DONE` when no item is left or the asker asked to stop, `MORE` when the stretch is used up,
    `WAIT` while an item of an earlier stretch may still be in flight, `SUPERSEDED` when another
    stretch was queued in this one's place (or the batch is paused), `CANCELLED` when this run
    task was cancelled or taken for lost, `PAUSE_TRIAL` after the first items of a large batch,
    `PAUSE_FAILURES` after `FAIL_STREAK` items in a row without success; `Halt` when the rest
    cannot run, `ItemCrashed` when sending an item raised.

    Items after the first of the stretch wait the system's `batch_interval_ms` before they go
    out; when the stretch has no time left for the wait it ends with `MORE`, and `next_chunk`
    queues the next stretch that much later (the first item of a stretch waits in the queue)."""
    session = pctx.session
    now = utcnow()
    # 上一段发出后没写回结果的：过了宽限仍没有结果才记为结果未知，不重发。还在宽限内的可能仍
    # 在途（那一段的进程被判失联但其实还活着），等它写回，不抢着下结论，也不同时再发别的项。
    await session.execute(
        update(SystemBatchItem)
        .where(
            SystemBatchItem.batch_id == state.id,
            SystemBatchItem.status == "sending",
            or_(
                SystemBatchItem.started_at.is_(None),
                SystemBatchItem.started_at < now - timedelta(seconds=SEND_GRACE),
            ),
        )
        .values(status="unknown", finished_at=now)
    )
    await session.commit()
    in_flight = await session.scalar(
        select(func.count())
        .select_from(SystemBatchItem)
        .where(SystemBatchItem.batch_id == state.id, SystemBatchItem.status == "sending")
    )
    if in_flight:
        return WAIT
    system, loaded, op = await checked(pctx, state)
    meta = state.state
    gap = system.batch_interval_ms / 1000
    # 这一段开始时已处理的项数：段内的进度按它加上本段处理的项数算，不必每项都去数。
    done_before = int(meta["count"]) - int(await _pending_count(session, state.id))
    started = clock()
    handled = 0
    while handled < CHUNK_ITEMS and clock() - started < CHUNK_SECONDS:
        if cancelled():
            return CANCELLED
        await session.refresh(state)
        if not _current(state, chunk):
            return SUPERSEDED
        if state.state.get(STOP):
            return DONE
        trial = state.state.get("trial")
        if trial and await _handled(session, state.id) >= int(trial):
            return PAUSE_TRIAL
        item = await session.scalar(
            select(SystemBatchItem)
            .where(SystemBatchItem.batch_id == state.id, SystemBatchItem.status == "pending")
            .order_by(SystemBatchItem.seq)
            .limit(1)
            # 万一仍有两段同时在跑，也不会把同一项发两次。
            .with_for_update(skip_locked=True)
        )
        if item is None:
            return DONE
        request = json.loads(cipher.decrypt(item.request_enc, _aad(state.id, item.seq)))
        call = proxy.Call(
            system=meta["system"],
            operation_id=meta["operation_id"],
            path_params=request.get("path_params"),
            query=request.get("query"),
            body=request.get("body"),
        )
        prepared = await proxy.build(pctx, system, loaded, op, call)
        if isinstance(prepared, dict):
            raise Halt(str(prepared.get("message") or prepared.get("error")))
        item.status = "sending"
        item.started_at = utcnow()
        await session.commit()
        seq = item.seq
        try:
            result = await proxy.send(pctx, prepared, key=item.idempotency_key, prefix="confirmed_")
        except Exception as exc:
            raise ItemCrashed(seq, type(exc).__name__) from exc
        if result.get("error") == "token_unavailable":
            # 令牌没签出来，请求没有发出。
            item.status = "pending"
            item.started_at = None
            await session.commit()
            raise Halt(str(result.get("message")))
        if result.get("ok"):
            item.status = "ok"
        elif result.get("error") == "system_unreachable":
            # 发出后没等到响应的，业务系统可能已经处理：结果未知，不算失败。
            item.status = "unknown" if result.get("maybe_sent") else "unreachable"
        else:
            item.status = "http_error"
        status = result.get("status")
        item.status_code = status if isinstance(status, int) else None
        item.brief_enc = cipher.encrypt(_brief(result), _aad(state.id, item.seq))
        item.finished_at = utcnow()
        failed = item.status in NOT_OK
        await session.commit()
        handled += 1
        if failed and await _streak(session, state):
            return PAUSE_FAILURES
        await progress(session, state, chunk=chunk, done=done_before + handled)
        if gap:
            # 限速：还有下一项才等；这一段剩下的时间不够等，就交给下一段延后再跑。
            if not await _pending(session, state.id):
                return DONE
            if clock() - started + gap >= CHUNK_SECONDS:
                break
            # 等待时不挂着事务。
            await session.commit()
            await _sleep(gap)
    return MORE if await _pending_count(session, state.id) else DONE


async def _pending_count(session: AsyncSession, batch_id: uuid.UUID) -> int:
    left = await session.scalar(
        select(func.count())
        .select_from(SystemBatchItem)
        .where(SystemBatchItem.batch_id == batch_id, SystemBatchItem.status == "pending")
    )
    return int(left or 0)


def progress_due(meta: dict[str, Any], done: int, now: datetime) -> bool:
    """Whether the progress card is due another update (see `PROGRESS_MIN_SECONDS`)."""
    try:
        last = aware_utc(datetime.fromisoformat(str(meta.get("progress_at"))))
    except ValueError:
        return True
    elapsed = (now - last).total_seconds()
    if elapsed < PROGRESS_MIN_SECONDS:
        return False
    if elapsed >= PROGRESS_MAX_SECONDS:
        return True
    step = max(1, math.ceil(int(meta["count"]) * PROGRESS_STEP))
    return done - int(meta.get("progress_done") or 0) >= step


async def _show_progress(
    session: AsyncSession, locked: InteractionState, found: Counter[str], now: datetime
) -> None:
    """Update the progress card of `locked` (locked by the caller) and note when."""
    total = int(locked.state["count"])
    n = int(locked.state.get("progress_n") or 0) + 1
    locked.state = {
        **locked.state,
        "progress_at": now.isoformat(),
        "progress_done": total - found["pending"] - found["sending"],
        "progress_n": n,
    }
    await confirm.update_card(
        session,
        locked,
        progress_card(locked, found),
        # 旧版本按段号用 `:progress:{段号}`：换个前缀，跨发布还在跑的批次不会撞键漏刷。
        dedupe_key=f"{locked.state.get('card_key')}:progress-n:{n}",
    )


async def progress(
    session: AsyncSession, state: InteractionState, *, chunk: int, done: int
) -> None:
    """Within a stretch: update the progress card when it is due."""
    now = utcnow()
    if not progress_due(state.state, done, now):
        return
    # 和停止按钮等改同一行：先锁住再改，别把别人刚写的状态覆盖掉。
    locked = await session.get(
        InteractionState, state.id, with_for_update=True, populate_existing=True
    )
    if _current(locked, chunk):
        assert locked is not None
        await _show_progress(session, locked, await counts(session, locked.id), now)
    await session.commit()


async def _pending(session: AsyncSession, batch_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(SystemBatchItem.seq)
            .where(SystemBatchItem.batch_id == batch_id, SystemBatchItem.status == "pending")
            .limit(1)
        )
    )


async def next_chunk(session: AsyncSession, state: InteractionState, *, chunk: int) -> bool:
    """Queue the next run task and show the progress; False (nothing done) when this run task
    is no longer the batch's current one."""
    locked = await session.get(
        InteractionState, state.id, with_for_update=True, populate_existing=True
    )
    if not _current(locked, chunk):
        return False
    assert locked is not None
    locked.state = {**locked.state, "chunk": chunk + 1}
    # 限速：下一段的第一项也要和这一段的最后一项隔开。
    await _enqueue(session, locked, chunk + 1, delay=await interval(session, locked))
    found = await counts(session, locked.id)
    now = utcnow()
    if progress_due(locked.state, int(locked.state["count"]) - found["pending"], now):
        await _show_progress(session, locked, found, now)
    return True


async def _streak(session: AsyncSession, state: InteractionState) -> bool:
    """The last `FAIL_STREAK` items handled since the batch last (re)started all went wrong,
    and there is still something left to run."""
    since = state.state.get("streak_since")
    stmt = (
        select(SystemBatchItem.status)
        .where(
            SystemBatchItem.batch_id == state.id,
            SystemBatchItem.status.in_(("ok", *NOT_OK)),
        )
        .order_by(SystemBatchItem.finished_at.desc(), SystemBatchItem.seq.desc())
        .limit(FAIL_STREAK)
    )
    if since:
        stmt = stmt.where(SystemBatchItem.finished_at > datetime.fromisoformat(str(since)))
    last = list(await session.scalars(stmt))
    if len(last) < FAIL_STREAK or any(status == "ok" for status in last):
        return False
    left = await session.scalar(
        select(func.count())
        .select_from(SystemBatchItem)
        .where(SystemBatchItem.batch_id == state.id, SystemBatchItem.status == "pending")
    )
    return bool(left)


def _item_line(item: SystemBatchItem, request: dict[str, Any], brief: str) -> str:
    code = f"（{item.status_code}）" if item.status_code else ""
    text = f"第 {item.seq} 项{_named(request)}：{LABELS[item.status]}{code}"
    return text + (" " + confirm._plain(brief, 120) if brief and item.status != "ok" else "")


def _decide(origin: int, state_id: uuid.UUID, left: int) -> dict[str, Any]:
    """The two buttons of a paused batch."""
    return {
        "tag": "column_set",
        "columns": [
            {
                "tag": "column",
                "width": "auto",
                "elements": [_button(origin, state_id, f"继续执行剩余 {left} 项", GO, "primary")],
            },
            {
                "tag": "column",
                "width": "auto",
                "elements": [_button(origin, state_id, "停止，剩余的不执行", STOP, "default")],
            },
        ],
    }


async def notify(session: AsyncSession, state: InteractionState, tag: str, text: str) -> None:
    """Tell the asker in the chat that the batch needs them or has ended: a card update alone
    notifies nobody. In a group the asker is mentioned."""
    meta = state.state
    open_id = str(meta.get("open_id") or "")
    mention = f"<at id={open_id}></at> " if open_id and meta.get("chat_type") == "group" else ""
    await outbox.add(
        session,
        bot_id=state.bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=f"{confirm.card_key(KIND, state.id, 0)}:notice:{tag}",
        target={"chat_id": meta["chat_id"]},
        payload={
            "card": {
                "schema": "2.0",
                "body": {"elements": [{"tag": "markdown", "content": mention + text}]},
            }
        },
    )


async def pause(
    session: AsyncSession, cipher: Cipher, state: InteractionState, reason: str, *, chunk: int
) -> bool:
    """Stop queueing stretches until the asker decides: show what happened so far with
    "carry on" and "stop". False (nothing done) when this run task is no longer current."""
    locked = await session.get(
        InteractionState, state.id, with_for_update=True, populate_existing=True
    )
    if not _current(locked, chunk):
        return False
    assert locked is not None
    now = utcnow()
    meta = {**locked.state, "paused": {"reason": reason, "at": now.isoformat()}}
    meta.pop("trial", None)
    locked.state = meta
    rows = await _rows(session, cipher, locked.id)
    found = Counter(item.status for item, _ in rows)
    total = int(meta["count"])
    handled = [(item, request) for item, request in rows if item.status != "pending"]

    def line(item: SystemBatchItem, request: dict[str, Any]) -> str:
        brief = cipher.decrypt(item.brief_enc, _aad(locked.id, item.seq)) if item.brief_enc else ""
        return _item_line(item, request, brief)

    if reason == PAUSE_TRIAL:
        title, template = f"已试跑 {len(handled)} 项，等你确认", "orange"
        lines = [
            f"先执行了前 {len(handled)} 项：{tally(found)}。",
            *(line(item, request) for item, request in handled),
            "",
            "请到业务系统核对这几项的实际结果，确认无误后再继续。",
        ]
        told = f"批量操作已试跑前 {len(handled)} 项并暂停，请核对后在卡片上选择继续或停止。"
    else:
        title, template = f"连续 {FAIL_STREAK} 项没有成功，已暂停", "red"
        wrong = [(item, request) for item, request in handled if item.status in NOT_OK]
        lines = [
            f"{bar(len(handled), total)}",
            f"已处理 {len(handled)} / {total}：{tally(found)}。",
            f"最近 {FAIL_STREAK} 项都没有成功，业务系统可能出了问题。最近一项：",
            *(line(item, request) for item, request in wrong[-1:]),
        ]
        told = f"批量操作连续 {FAIL_STREAK} 项没有成功，已暂停，请在卡片上选择继续或停止。"
    minutes = int(PAUSE_TTL.total_seconds() // 60)
    lines.append(f"{minutes} 分钟内没有选择会自动结束，剩余各项不执行。")
    body = {
        "schema": "2.0",
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {
                "tag": "plain_text",
                "content": confirm._plain(f"{meta['system_name']} · {_title(meta)}"),
            },
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)}},
                _decide(int(meta["origin_task_id"]), locked.id, found["pending"]),
            ]
        },
    }
    await confirm.update_card(
        session, locked, body, dedupe_key=f"{meta.get('card_key')}:paused:{chunk}"
    )
    await notify(session, locked, f"paused:{chunk}", told)
    return True


async def _restart(session: AsyncSession, state: InteractionState, meta: dict[str, Any]) -> None:
    """Queue the next stretch of a batch that was paused or finished, pacing from now."""
    now = utcnow()
    chunk = int(meta.get("chunk") or 1) + 1
    for key in (
        "paused",
        "finished_at",
        STOP,
        "stopped_by",
        "revived",
        "revived_at",
        "revived_done",
    ):
        meta.pop(key, None)
    meta.update(
        chunk=chunk,
        started_at=now.isoformat(),
        started_done=await _handled(session, state.id),
        # 之前的失败不算进新的连续失败里。
        streak_since=now.isoformat(),
    )
    state.state = meta
    state.expires_at = None
    await _enqueue(session, state, chunk)


async def go(session: AsyncSession, state: InteractionState) -> bool:
    """The asker chose to carry on after a pause. `state` is locked by the caller."""
    if state.status != "submitted" or not state.state.get("paused"):
        return False
    await _restart(session, state, dict(state.state))
    return True


async def requeue(session: AsyncSession, state: InteractionState, verb: str, now: datetime) -> int:
    """After a batch ended: run the items not run yet (`CONTINUE`) or the failed ones (`RETRY`)
    again, while the batch is kept. Items whose outcome is unknown are never sent again. Returns
    how many were queued. `state` is locked by the caller."""
    kept = state.expires_at is not None and aware_utc(state.expires_at) > now
    if state.status != "submitted" or not state.state.get("finished_at") or not kept:
        return 0
    again = ("skipped",) if verb == CONTINUE else FAILED
    values: dict[str, Any] = {
        "status": "pending",
        "status_code": None,
        "brief_enc": None,
        "started_at": None,
        "finished_at": None,
    }
    result = await session.execute(
        update(SystemBatchItem)
        .where(SystemBatchItem.batch_id == state.id, SystemBatchItem.status.in_(again))
        .values(**values)
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    if not count:
        return 0
    if verb == RETRY:
        # 重试是一次新的请求：换新的幂等键，免得业务系统把上次失败的结果原样返回。
        # 收尾后没有别的待执行项，这时的 pending 就是刚放回来的这些。
        retried = await session.scalars(
            select(SystemBatchItem)
            .where(SystemBatchItem.batch_id == state.id, SystemBatchItem.status == "pending")
            .execution_options(populate_existing=True)
        )
        for item in retried:
            item.idempotency_key = str(uuid.uuid4())
    meta = dict(state.state)
    meta["round"] = int(meta.get("round") or 0) + 1
    await _restart(session, state, meta)
    return count


async def mark_unknown(
    session: AsyncSession, cipher: Cipher, batch_id: uuid.UUID, seq: int, kind: str
) -> None:
    """An item whose sending raised: it may or may not have left, so it is never sent again."""
    item = await session.get(SystemBatchItem, (batch_id, seq), populate_existing=True)
    if item is None or item.status != "sending":
        return
    item.status = "unknown"
    item.finished_at = utcnow()
    item.brief_enc = cipher.encrypt(
        _canon(
            {
                "error": "platform_error",
                "message": f"平台处理这一项时出错（{kind}），不确定请求有没有发出。",
            }
        ),
        _aad(batch_id, seq),
    )
    log.warning("systems_batch_item_crashed", batch_id=str(batch_id), seq=seq, error_type=kind)


async def resume(session: AsyncSession, origin: Task, state: InteractionState, note: str) -> None:
    """Continue the conversation that staged the batch with its outcome (once per batch)."""
    await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=origin.bot_id,
            kind="chat",
            user_id=origin.user_id,
            session_key=origin.session_key,
            inbound_event_id=origin.inbound_event_id,
            # 结束后又继续、重试的，每次结束各续接一轮。
            dedupe_key=f"systems-write:{state.id}:resume"
            + (f":{round_}" if (round_ := int(state.state.get("round") or 0)) else ""),
            payload={
                **carried(origin.payload, confirm.RESULT_KEY),
                confirm.RESULT_KEY: note,
                "serialize_session": True,
                # 用户刚点完确认卡，不再问「还要不要做」。
                "stale_confirmed": True,
            },
        ),
    )


def _stopper(meta: dict[str, Any]) -> str:
    return "管理员在管理台停止了执行" if meta.get("stopped_by") == "admin" else "按发起人的要求停止"


def phase(state: InteractionState) -> str:
    """Where a batch stands, for the console: awaiting (the asker's confirmation), running,
    paused, finished, not_started (confirmed but refused or never started), cancelled, expired."""
    if state.status != "submitted":
        return "awaiting" if state.status == "open" else state.status
    meta = state.state
    if meta.get("finished_at"):
        return "finished"
    if meta.get("paused"):
        return "paused"
    return "running" if "chunk" in meta else "not_started"


async def admin_stop(session: AsyncSession, cipher: Cipher, state: InteractionState) -> bool:
    """A console administrator stops a running or paused batch: the rest is not run and the asker
    is told who stopped it. `state` is locked by the caller. False when there is nothing to stop."""
    if phase(state) not in ("running", "paused") or state.state.get(STOP):
        return False
    paused = bool(state.state.get("paused"))
    state.state = {**state.state, STOP: True, "stopped_by": "admin"}
    if paused:
        await session.flush()
        await _give_up(session, state, None, cipher=cipher)
    return True


RESULT_HEADER = [*DETAIL_HEADER, "结果", "状态码", "摘要"]


async def finish(
    session: AsyncSession,
    cipher: Cipher,
    state: InteractionState,
    halted: str | None,
    *,
    chunk: int,
) -> str | None:
    """Mark the rest as not run, show the outcome and return the note for the model; None
    (nothing done) when this run task is no longer the batch's current one."""
    now = utcnow()
    locked = await session.get(
        InteractionState, state.id, with_for_update=True, populate_existing=True
    )
    if not _current(locked, chunk, paused_too=True):
        return None
    assert locked is not None
    await session.execute(
        update(SystemBatchItem)
        .where(SystemBatchItem.batch_id == locked.id, SystemBatchItem.status == "pending")
        .values(status="skipped", finished_at=now)
    )
    # 记了「正在发送」却没有结果的，可能已经发出：结果未知，绝不能写成没有执行。
    await session.execute(
        update(SystemBatchItem)
        .where(SystemBatchItem.batch_id == locked.id, SystemBatchItem.status == "sending")
        .values(status="unknown", finished_at=now)
    )
    meta = locked.state
    rows = await _rows(session, cipher, locked.id)
    found = Counter(item.status for item, _ in rows)
    briefs = {
        item.seq: cipher.decrypt(item.brief_enc, _aad(locked.id, item.seq))
        if item.brief_enc
        else ""
        for item, _ in rows
    }
    url = await _csv_link(
        session,
        f"{meta['operation_id']}-结果.csv",
        RESULT_HEADER,
        [
            [*_detail(item.seq, request), LABELS[item.status], item.status_code, briefs[item.seq]]
            for item, request in rows
        ],
        expires_at=None,
    )
    failed = [
        (item, request)
        for item, request in rows
        if item.status in ("http_error", "unreachable", "unknown")
    ]
    total = int(meta["count"])
    all_ok = found["ok"] == total
    stopped = bool(meta.get(STOP))
    lines = [f"共 {total} 项：{tally(found)}。"]
    if halted:
        lines.append(f"中途停下：{halted}。剩下的没有执行。")
    elif stopped:
        lines.append(f"{_stopper(meta)}，剩下的没有执行。")
    if found["unknown"]:
        lines.append("结果未知的项可能已经生效，请先到业务系统核对，不要直接重做。")
    for item, request in failed[:FAILURES_ON_CARD]:
        code = f"（{item.status_code}）" if item.status_code else ""
        lines.append(
            f"第 {item.seq} 项{_named(request)}：{LABELS[item.status]}{code} "
            + confirm._plain(briefs[item.seq], 120)
        )
    if len(failed) > FAILURES_ON_CARD:
        lines.append(f"…另有 {len(failed) - FAILURES_ON_CARD} 项没有成功，见结果下载")
    origin_id = int(meta["origin_task_id"])
    again: list[dict[str, Any]] = []
    retryable = found["http_error"] + found["unreachable"]
    if found["skipped"]:
        label = f"继续执行剩余 {found['skipped']} 项"
        again.append(_button(origin_id, locked.id, label, CONTINUE, "primary"))
    if retryable:
        label = f"重试失败的 {retryable} 项"
        again.append(_button(origin_id, locked.id, label, RETRY, "default"))
    if again:
        lines.append("")
        lines.append(
            "下面的按钮 24 小时内有效，只有发起人能点；点了之后平台先复查权限再执行，"
            "已经成功和结果未知的项不会再做。"
        )
    round_ = int(meta.get("round") or 0)
    body = {
        "schema": "2.0",
        "header": {
            "template": "green" if all_ok else "red",
            "title": {"tag": "plain_text", "content": "已完成" if all_ok else "没有全部完成"},
            "subtitle": {
                "tag": "plain_text",
                "content": confirm._plain(f"{meta['system_name']} · {_title(meta)}"),
            },
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)}},
                *_link(url, "下载全部结果（CSV）"),
                *(
                    [
                        {
                            "tag": "column_set",
                            "columns": [
                                {"tag": "column", "width": "auto", "elements": [button]}
                                for button in again
                            ],
                        }
                    ]
                    if again
                    else []
                ),
            ]
        },
    }
    await confirm.update_card(
        session, locked, body, dedupe_key=f"{meta.get('card_key')}:result:{round_}"
    )
    meta = {**meta, "finished_at": now.isoformat()}
    meta.pop("paused", None)
    meta.pop("trial", None)
    locked.state = meta
    locked.expires_at = now + confirm.KEEP
    if meta.get("chat_type") == "group":
        # 私聊里续接的那一轮回复本身就是提醒；群里点名告诉发起人一声。
        await notify(
            session,
            locked,
            f"finished:{round_}",
            f"批量操作已结束，共 {total} 项：{tally(found)}。详情见上面的结果卡片。",
        )
    note = [
        f"用户已确认，平台按确认内容逐项执行了 {meta['system']}.{meta['operation_id']}，"
        f"共 {total} 项：{tally(found)}。"
    ]
    if halted:
        note.append(f"中途停下：{halted}。剩下的没有执行。")
    elif stopped:
        note.append(f"{_stopper(meta)}，剩下的没有执行。")
    for item, request in failed[:FAILURES_TO_MODEL]:
        note.append(f"第 {item.seq} 项{_named(request)}：{LABELS[item.status]} " + briefs[item.seq])
    if url:
        note.append("完整结果已在卡片上给用户提供 CSV 下载。")
    if found["unknown"]:
        note.append(
            "结果未知的项可能已经在业务系统里生效：先用读操作逐项核对实际状态并告诉用户，"
            "不要直接重做。"
        )
    note.append(
        "抽查读回几项关键字段核对后如实汇报。"
        if all_ok
        else "没有成功和未执行的项不要自行重试，先告诉用户。"
    )
    return "\n".join(note)


async def _handled(session: AsyncSession, batch_id: uuid.UUID) -> int:
    done = await session.scalar(
        select(func.count())
        .select_from(SystemBatchItem)
        .where(SystemBatchItem.batch_id == batch_id, SystemBatchItem.status != "pending")
    )
    return int(done or 0)


async def _give_up(
    session: AsyncSession,
    state: InteractionState,
    reason: str | None,
    *,
    cipher: Cipher | None = None,
) -> None:
    """Finish a batch outside a run task (the rest not run) and continue the conversation."""
    log.warning("systems_batch_gave_up", batch_id=str(state.id))
    meta = state.state
    note = await finish(
        session,
        cipher or get_settings().build_cipher(),
        state,
        reason,
        chunk=int(meta.get("chunk") or 0),
    )
    origin = await session.get(Task, int(meta["origin_task_id"]))
    if note is not None and origin is not None:
        await resume(session, origin, state, note)


async def revive(session: AsyncSession, now: datetime) -> int:
    """A confirmed batch whose run task is gone (worker lost, handler crashed) without finishing:
    queue the next stretch, which skips everything already handled. After `MAX_REVIVES` in a row
    with no item handled in between, finish the batch instead (the rest not run) and tell the
    asker, so it never stays "running" for good. Returns how many batches were handled."""
    running = list(
        await session.scalars(
            select(InteractionState.id).where(
                InteractionState.kind == KIND,
                InteractionState.status == "submitted",
                InteractionState.expires_at.is_(None),
            )
        )
    )
    handled = 0
    for batch_id in running:
        live = await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.kind == RUN_KIND,
                Task.status.in_(tasks.OPEN),
                Task.dedupe_key.like(f"systems-batch:{batch_id}:run:%"),
            )
        )
        if live:
            continue
        state = await session.get(
            InteractionState, batch_id, with_for_update=True, populate_existing=True
        )
        if state is None or state.status != "submitted" or state.state.get("finished_at"):
            continue
        meta = state.state
        paused = meta.get("paused")
        if paused:
            # 等发起人选继续还是停止：不补排；一直没人选就到点结束。
            since = aware_utc(datetime.fromisoformat(str(paused["at"])))
            if now - since < PAUSE_TTL:
                continue
            minutes = int(PAUSE_TTL.total_seconds() // 60)
            await _give_up(session, state, f"暂停后 {minutes} 分钟没有选择继续")
            handled += 1
            continue
        try:
            last = aware_utc(datetime.fromisoformat(str(meta.get("revived_at"))))
        except ValueError:
            last = None
        if last is not None and (now - last).total_seconds() < REVIVE_GAP:
            continue
        done = await _handled(session, batch_id)
        # 上次补排之后有进展，就不算连续失败。
        revived = 0 if done > int(meta.get("revived_done", -1)) else int(meta.get("revived") or 0)
        chunk = int(meta.get("chunk") or 1) + 1
        # 段号总要往前走：被判丢失却还活着的那一段据此发现自己已被接手。
        state.state = {
            **meta,
            "chunk": chunk,
            "revived": revived + 1,
            "revived_at": now.isoformat(),
            "revived_done": done,
        }
        await session.flush()
        handled += 1
        if revived >= MAX_REVIVES:
            await _give_up(session, state, "执行任务多次意外中断，已停止")
            continue
        await _enqueue(session, state, chunk)
        log.warning("systems_batch_revived", batch_id=str(batch_id), chunk=chunk)
    await session.commit()
    return handled
