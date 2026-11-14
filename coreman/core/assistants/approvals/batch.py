"""个人助理一轮里的飞书写：先暂存，本轮结束时一起交给所有者人工确认。

写操作都要所有者人工确认，确认方式按他在不在场分：

- 在场（他本人在与助理的私聊里发来的这一轮，`systems_confirm.confirmable_origin` 是私聊）：本轮
  正常结束时给他发一张确认卡，按顺序列出这一轮暂存的全部飞书写；他点确认后平台按暂存的内容
  原样、按顺序执行（`runtime/worker/assistant_write_cards.py`），结果作为续接的一轮投回。
- 无人在场（交回、结论投回、审批结果回合，`kinds.unattended`）：本轮正常结束时合成一张审批单
  提交审批系统（`service.submit_batch`），批准后由执行任务按顺序执行。

两种都按任务号一批（`interaction_states`，kind `assistant_write`，`mode` 区分），结构照云服务写
操作的确认卡（`cloud_proxy.confirm`）。暂存的内容只在 `interaction_states.state` 里，助理的行落库
前整块用所有者的数据密钥加密；附件当场复制进临时桶（中转编号一小时就过期）。没正常结束的一轮
整批作废（附件随之删除）；同一个任务重跑时上一次暂存的作废重来；没发出去的批次由
`systems_confirm.sweep` 清掉。判断在不在场只看服务端的任务类型与入站事件，不看模型传的任何参数。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants.approvals import attachments, checks, execute, service, tower
from coreman.core.assistants.records import inactive_assistant, is_assistant
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, InteractionState, Task
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.systems_catalog import confirm as systems_confirm
from coreman.core.timeutils import utcnow

KIND = "assistant_write"
CARD_EVENT = KIND
# 卡片答完后续接的那一轮，任务载荷里放结果的键。
RESULT_KEY = "assistant_write_result"
CARD = "card"
TOWER = "tower"
MAX_ITEMS = service.MAX_ITEMS
# 卡片正文（字符）与整张卡序列化后（字节）的上限，同云服务写操作的确认卡：一个字都不截，
# 放不下就不暂存。
CARD_TEXT_MAX = systems_confirm.CARD_TEXT_MAX
CARD_BYTES_MAX = 24_000
TOO_LARGE = "too_large_to_review"

CARD_HINT = (
    "已暂存，没有执行。本轮结束时平台给所有者发一张确认卡，按顺序列出本轮暂存的全部飞书操作；"
    "他点确认后平台按暂存的内容原样、按顺序执行，并在本会话里告诉你每一项的结果。不要重试，"
    "也不要让所有者去审批系统；告诉他需要在卡片上确认，不要说已经完成。"
)
TOWER_HINT = (
    "已暂存，没有执行。本轮结束时平台把本轮暂存的飞书操作合成一张审批单提交给所有者，批准后按"
    "顺序执行，结果在本会话里告诉你。不要重复提交，继续处理别的事。"
)
CLOSED = "本轮的写操作已经交出去确认，不能再追加。请所有者另发一条消息再做。"
BATCH_FULL = (
    f"一轮最多暂存 {MAX_ITEMS} 个飞书写操作，这一个没有加入，也没有执行。剩下的另起一轮再做"
    "（所有者在场时请他另发一条消息）。"
)
TOO_LARGE_HINT = (
    "这次操作的内容太长，确认卡上放不下完整内容（连同本轮已暂存的写操作），没有暂存，也没有"
    "执行。拆成能在卡片上完整展示的小操作再调用，或让所有者在另一条消息里接着做。"
)


async def _mode(session: AsyncSession, scope: Scope) -> str | None:
    """这一轮的飞书写怎么确认：`card`、`tower`，或 None（照旧当场提交审批单）。

    锁住任务行，同一轮的暂存一次一个。
    """
    bot = scope.bot
    if not is_assistant(bot) or bot.owner_id != scope.user_id or scope.scheduled:
        return None
    if scope.unattended:
        await session.get(Task, scope.task.id, with_for_update=True, populate_existing=True)
        return TOWER
    origin = await systems_confirm.confirmable_origin(session, scope.task.id, scope.user_id)
    if (
        origin is None
        or origin[1] != "single"
        or origin[0] != scope.chat_id
        or not bot.enabled
        or inactive_assistant(bot)
    ):
        return None
    return CARD


def _store() -> attachments.Store | None:
    return attachments.store(get_settings())


def _visible(text: str) -> str:
    from coreman.core.assistants.approvals import proxied  # proxied 依赖面广，延迟导入

    return proxied._visible(text)


def card_text(items: list[dict[str, Any]]) -> str:
    """按暂存的内容渲染：每一项是它单独提交审批时的正文（平台按实际参数生成），完整展示。

    控制、格式字符与 `\\n` 以外的行、段分隔符转义成可见的 `\\uXXXX`（同无人在场的代理写审批单），
    一个值造不出独立的一行，也颠倒不了显示。
    """
    rows: list[str] = []
    for index, item in enumerate(items, 1):
        rows.append(f"{index}. {_visible(str(item['abstract']))}")
        rows += ["   " + line for line in _visible(str(item["content"])).split("\n")]
    return "\n".join(rows)


def _links(items: list[dict[str, Any]], store: attachments.Store | None) -> list[str]:
    """附件的查看链接（与卡片同时失效）。链接文字是平台写的，不放文件名。"""
    if store is None:
        return []
    seconds = int(systems_confirm.TTL.total_seconds())
    out: list[str] = []
    for index, item in enumerate(items, 1):
        refs = attachments.references(list(item.get("calls") or []))
        for number, ref in enumerate(refs, 1):
            url = attachments.link(store, ref, seconds)
            out.append(f"[查看第 {index} 项的附件 {number}]({url})")
    return out


def card(
    origin_task_id: int,
    state_id: uuid.UUID,
    items: list[dict[str, Any]],
    store: attachments.Store | None,
) -> dict[str, Any]:
    def button(label: str, verb: str, kind: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": kind,
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "task_id": f"personal:{origin_task_id}",
                        "event_key": CARD_EVENT,
                        "level": f"{verb}:{state_id}",
                    },
                }
            ],
        }

    # 内容来自对话：纯文本，@ 与链接都不生效。
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": card_text(items)}}
    ]
    links = _links(items, store)
    if links:
        elements.append({"tag": "markdown", "content": "\n".join(links)})
    elements += [
        button("确认执行", "confirm", "primary"),
        button("取消", "cancel", "default"),
        {
            "tag": "markdown",
            "content": (
                "<font color='grey'>只有你本人能操作。平台以你的身份按上面的内容原样、按顺序执行，"
                "执行前会重新核对会话与授权，遇到失败即停止。60 分钟内有效，过期后可以重新发起。"
                "</font>"
            ),
        },
    ]
    return {
        "schema": "2.0",
        "task_id": f"personal:{origin_task_id}",
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"确认：执行 {len(items)} 个飞书操作"},
        },
        "body": {"elements": elements},
    }


def fits(items: list[dict[str, Any]], store: attachments.Store | None) -> bool:
    """整批放得进一张卡：正文与序列化后的卡片都不超上限。"""
    if len(card_text(items)) > CARD_TEXT_MAX:
        return False
    whole = json.dumps(card(0, uuid.UUID(int=0), items, store), ensure_ascii=False)
    return len(whole.encode()) <= CARD_BYTES_MAX


async def discard_files(value: dict[str, Any]) -> None:
    """作废的批次留在临时桶里的附件：删掉（删不掉由桶的自动清理兜底）。"""
    keys = [
        ref["key"]
        for item in list(value.get("items") or [])
        for ref in attachments.references(list(item.get("calls") or []))
    ]
    store = _store() if keys else None
    if store is not None:
        await attachments.delete(store, keys)


async def propose(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    tool: str,
    arguments: dict[str, Any],
    *,
    cfg: tower.Config | None,
    files: FileRelay | None,
) -> dict[str, Any] | None:
    """把一次飞书写暂存进本轮的批次；不执行，也不当场提交。调用方提交事务。

    返回 None 表示这一轮不在这里暂存（例如不是所有者本人私聊发来的在场轮次），调用方照旧当场
    提交审批单。校验、重新读会话、正文的生成与当场提交时完全相同（`service._stage`）。

    Raises:
        PersonalError: 重新读会话时所有者的授权失效
    """
    mode = await _mode(session, scope)
    if mode is None:
        return None
    if mode == TOWER:
        requester = await service._requester(session, scope, cfg)
        if isinstance(requester, dict):
            return requester
    staged = await service._stage(
        session, cipher, scope, tool, arguments, files, links=mode == TOWER
    )
    if isinstance(staged, dict):
        return staged
    bot, task = scope.bot, scope.task
    existing = await service._duplicate(session, bot.id, staged.args_hash)
    if existing is not None:
        live = existing.status in service.LIVE
        return {
            "status": "pending_approval" if live else "already_executed",
            "approval_id": str(existing.id),
            "duplicate": True,
        }
    state = await systems_confirm._batch(session, bot.id, task.id, kind=KIND)
    value: dict[str, Any] = dict(state.state) if state is not None else {}
    if state is not None and not value.get("sent") and value.get("attempt") != task.attempts:
        # 同一个任务重跑：上一次暂存的作废，不与这一次的混在一张卡或一张单里。
        await discard_files(value)
        value = {**value, "items": [], "count": 0, "attempt": task.attempts}
        state.state, state.status = value, "open"
    if state is not None and (state.status != "open" or value.get("sent")):
        return {"error": "confirmation_closed", "message": CLOSED}
    items: list[dict[str, Any]] = list(value.get("items") or [])
    pending = {"pending_confirmation": CARD_HINT, "pending_approval": TOWER_HINT}
    status = "pending_confirmation" if mode == CARD else "pending_approval"
    if any(item.get("args_hash") == staged.args_hash for item in items):
        # 同一轮里重复调用同一份内容：不重复暂存，卡片或审批单上也只出现一次。
        return {"status": status, "pending": len(items), "duplicate": True, "hint": pending[status]}
    if len(items) >= MAX_ITEMS:
        return {"error": "confirmation_batch_full", "message": BATCH_FULL}
    if (
        mode == TOWER
        and not items
        and await service.pending_count(session, bot.id) >= service.MAX_PENDING
    ):
        return dict(service.TOO_MANY)
    first = staged.calls[0]
    item = {
        "tool": tool,
        "calls": staged.calls,
        "abstract": staged.abstract,
        "content": staged.content,
        "background": staged.meta.background,
        "research": staged.meta.research,
        "topic": staged.meta.topic,
        "channel": staged.facts[0].channel,
        "chat_id": first["chat_id"],
        "args_hash": staged.args_hash,
        "has_files": bool(staged.uploads),
    }
    batch = [*items, item]
    if mode == CARD and not fits(batch, staged.store or _store()):
        return {
            "error": TOO_LARGE,
            "limit": {"text_chars": CARD_TEXT_MAX, "card_bytes": CARD_BYTES_MAX},
            "hint": TOO_LARGE_HINT,
        }
    try:
        await service._store_uploads(files, staged)
    except attachments.Unavailable:
        await service._discard(staged)
        return dict(service.FILE_STORE_DOWN)
    value = {
        "origin_task_id": task.id,
        "user_id": str(scope.user_id),
        "chat_id": scope.chat_id if mode == CARD else "",
        "mode": mode,
        "attempt": task.attempts,
        "session_key": task.session_key or "",
        "event_id": scope.event.id if scope.event is not None else None,
        "count": len(batch),
        "items": batch,
    }
    if state is None:
        session.add(
            InteractionState(
                bot_id=bot.id,
                kind=KIND,
                scope_key=str(task.id),
                state=value,
                status="open",
                # 发出的卡另有期限；没交出去的批次只比这一轮多活一会儿。
                expires_at=utcnow()
                + timedelta(seconds=bot.sse_timeout_seconds)
                + systems_confirm.TTL,
            )
        )
    else:
        state.state = value
    out: dict[str, Any] = {"status": status, "pending": len(batch), "hint": pending[status]}
    if mode == TOWER:
        # 还没有单号：本轮结束时才提交。
        out["staged"] = True
    return out


async def release(
    session: AsyncSession, *, bot_id: uuid.UUID, task_id: int, send: bool
) -> str | None:
    """一轮结束时：正常结束就把暂存的飞书写交出去确认，否则整批作废。调用方提交事务。

    在场的批次发确认卡，无人在场的合成审批单提交（`service.submit_batch`，提交完删掉批次）。
    返回交出去的方式（`card` / `tower`），什么都没交出去时为 None。
    """
    state = await systems_confirm._batch(session, bot_id, task_id, kind=KIND)
    if state is None or state.status != "open" or state.state.get("sent"):
        return None
    value = dict(state.state)
    items = list(value.get("items") or [])
    bot = await session.get(Bot, bot_id)
    usable = bot is not None and bot.enabled and not inactive_assistant(bot)
    if not send or not items or not usable:
        # 没正常结束、内容读不出来（所有者的数据密钥已销毁），或助理已停用：什么都不执行。
        state.status = "cancelled"
        await discard_files(value)
        return None
    assert bot is not None
    if value.get("mode") == CARD:
        body = card(task_id, state.id, items, _store())
        await systems_confirm.send_card(session, state, body, now=utcnow())
        return CARD
    await service.submit_batch(session, bot, value, cfg=tower.config(get_settings()))
    await session.delete(state)
    return TOWER


async def run_confirmed(
    factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    cipher: Cipher,
    bot: Bot,
    task: Task,
    items: list[dict[str, Any]],
    files: FileRelay | None,
) -> tuple[int, str] | list[tuple[list[dict[str, Any]], dict[str, Any] | None] | None]:
    """确认卡点了确认之后：整批复查，全部通过才按顺序原样执行，遇到失败即停。

    复查与批准后执行审批单的那一套相同：所有者对这个助理的授权仍连接着，逐项重新读会话、核对
    锚点与目标（`execute.check_calls`），附件取回并核对大小与 SHA-256。调用方已把批次记为已提交
    并提交了事务（并发点击、重复回调都执行不到这里第二次）。

    Returns:
        `(第几项, 原因代码)`：整批都没有执行；否则是各项的 `(结果, 失败)`，停下之后的项为 None
    """
    scope = await execute.owner_scope(session, bot, task)
    if scope is None:
        return 0, "authorization_required"
    blobs: dict[str, bytes] = {}
    for index, item in enumerate(items, 1):
        calls = list(item.get("calls") or [])
        try:
            await execute.check_calls(session, cipher, scope, str(item["tool"]), calls)
            await execute.fetch(files, calls, blobs)
        except (checks.Rejected, PersonalError, execute.Unrunnable, execute.Retry) as exc:
            return index, str(getattr(exc, "code", "") or type(exc).__name__)
    # 复查时可能刷新过令牌：先提交，执行在各自的短事务里（不与这个事务抢授权行的锁）。
    await session.commit()
    out: list[tuple[list[dict[str, Any]], dict[str, Any] | None] | None] = []
    stopped = False
    for item in items:
        if stopped:
            out.append(None)
            continue
        done = await execute.perform(
            factory,
            cipher,
            bot.id,
            task,
            str(item["tool"]),
            list(item.get("calls") or []),
            blobs,
            files,
        )
        out.append(done)
        stopped = done[1] is not None
    return out
