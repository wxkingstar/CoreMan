"""盯消息顺带的检查：每位助理每 5 分钟一次，不经模型。

产出是 `kind = 'reminder'` 的收件箱条目（与消息条目一起交回、一起记账、一起受预算约束），
或直接动作（撤回审批单、私信授权到期提醒）。去重靠收件箱的唯一索引 `(bot_id, kind,
message_id)`：提醒条目的 `message_id` 存的是去重键，真实的消息 id 在摘要里。摘要的形状见
`handover.py`。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import delivery, followups, sealed, sent, watch
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants import watch_feed as feed
from coreman.core.assistants.approvals import catalog, tower
from coreman.core.assistants.approvals import checks as anchors
from coreman.core.assistants.approvals import service as approvals
from coreman.core.bus import outbox
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantApproval,
    AssistantDelivery,
    AssistantInbox,
    AssistantMark,
    AssistantSettings,
    AssistantWatch,
    Bot,
    FeishuPersonalGrant,
)
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.logging import get_logger
from coreman.core.timeutils import aware_utc

log = get_logger(__name__)

# 「处理中」表情挂了这么久还没回，提醒助理。
MARK_STALE = timedelta(hours=4)
# 一次检查最多为多少条到期的待跟进入箱；多出来的下一次再入。
FOLLOWUP_BATCH = 20
# 一次 im.mget 最多查这么多条消息。
CHECK_BATCH = 20
# 交回之后这么久以内的消息还查「有没有被对方撤回」。
RECALL_WINDOW = timedelta(hours=24)
# 每位助理多久做一次顺带检查；一次 tick 最多认领多少位。
SWEEP_EVERY = timedelta(minutes=5)
BATCH = 40
# 飞书要求授权满这么久重新授权；到期前这么多天起每天在私聊里提醒一次。
REAUTH_AFTER = timedelta(days=365)
REAUTH_NOTICE_DAYS = 14
REAUTH_SOON = (
    "你的飞书授权还有 {days} 天满一年，到时需要重新授权，否则助理读不了你的消息、也办不了事。"
    "在这里跟助理说一声「重新授权飞书」就可以。"
)
REAUTH_OVERDUE = (
    "你的飞书授权已经满一年，需要重新授权，否则助理读不了你的消息、也办不了事。"
    "在这里跟助理说一声「重新授权飞书」就可以。"
)
# 复核会话得到的结论 → 撤回原因代码。锚点找不到（太早了，或被撤回而 mget 没认出来）按会话动过算。
_VERDICTS = {
    "owner_replied_after_anchor": "owner_replied",
    "newer_messages_after_anchor": "anchor_moved",
    "anchor_not_found": "anchor_moved",
}


async def add_reminder(
    session: AsyncSession,
    bot_id: uuid.UUID,
    key: str,
    *,
    level: str,
    summary: dict[str, Any],
    now: datetime,
    chat_id: str | None = None,
) -> bool:
    """入一条提醒；同一个 `key` 已经入过（不论现在什么状态）就什么都不做，返回 False。

    Args:
        key: 去重键，存进 `message_id`；带日期的键每天能入一次
        level: `immediate` 或 `batch`。两种都不设 `hold_until`（下一次投递就带上）；区别在
            预算用完之后：`batch` 留到次日
        summary: 提醒的内容，整块加密
    """
    values = await sealed.seal_values(
        session,
        AssistantInbox,
        bot_id,
        {
            "bot_id": bot_id,
            "kind": "reminder",
            "level": level,
            "chat_id": chat_id,
            "message_id": key,
            "message_at": now,
            "summary_enc": summary,
        },
    )
    inserted = await session.scalar(
        insert(AssistantInbox)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["bot_id", "kind", "message_id"],
            index_where=text("message_id IS NOT NULL"),
        )
        .returning(AssistantInbox.id)
    )
    return inserted is not None


async def followups_due(
    session: AsyncSession, bot_id: uuid.UUID, today: date, now: datetime
) -> int:
    """当天到期的待跟进各入一条提醒，每条每天至多一次；返回新入了几条。"""
    added = 0
    for row in (await followups.due(session, bot_id, today))[:FOLLOWUP_BATCH]:
        if sealed.is_locked(row):
            continue
        added += await add_reminder(
            session,
            bot_id,
            f"followup:{row.id}:{today.isoformat()}",
            level="batch",
            now=now,
            summary={
                "reminder": "followup_due",
                "label": followups.label(row.seq),
                "category": row.category,
                "due_on": row.due_on.isoformat() if row.due_on else None,
                "review_on": row.review_on.isoformat() if row.review_on else None,
                "title": row.title_enc,
            },
        )
    return added


async def stale_marks(session: AsyncSession, bot_id: uuid.UUID, today: date, now: datetime) -> int:
    """挂了 4 小时以上的「处理中」表情各入一条提醒，每条每天至多一次；返回新入了几条。"""
    rows = (
        await session.execute(
            select(AssistantMark.message_id, AssistantMark.chat_id, AssistantMark.created_at)
            .where(AssistantMark.bot_id == bot_id, AssistantMark.created_at <= now - MARK_STALE)
            .order_by(AssistantMark.created_at)
        )
    ).all()
    added = 0
    for message_id, chat_id, marked_at in rows:
        added += await add_reminder(
            session,
            bot_id,
            f"mark:{message_id}:{today.isoformat()}",
            level="batch",
            chat_id=chat_id,
            now=now,
            summary={
                "reminder": "mark_stale",
                "chat_id": chat_id,
                "message_id": message_id,
                "marked_at": marked_at.isoformat(),
            },
        )
    return added


async def reader(
    session: AsyncSession, cipher: Cipher, bot: Bot, prefs: AssistantSettings
) -> feed.Reader | None:
    """以所有者身份读飞书的入口；身份或授权对不上时返回 None（这一次不做要读飞书的检查）。"""
    scope, code = await watch.owner_scope(session, bot, prefs)
    if scope is None:
        log.info("assistant_delivery_checks_no_scope", bot_id=str(bot.id), code=code)
        return None
    return Call(session, cipher, scope)


def _ms(value: datetime) -> int:
    return int(aware_utc(value).timestamp() * 1000)


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def _chat_marks(session: AsyncSession, bot_id: uuid.UUID) -> dict[str, tuple[int, int]]:
    """盯消息的游标里每个会话的两个时刻（毫秒）：所有者上次发言、读到的最新一条消息。"""
    cursor = await session.scalar(
        select(AssistantWatch.cursor).where(AssistantWatch.bot_id == bot_id)
    )
    chats = cursor.get("chats") if isinstance(cursor, dict) else None
    return {
        str(chat): (_number(entry.get("o")), _number(entry.get("t")))
        for chat, entry in (chats if isinstance(chats, dict) else {}).items()
        if isinstance(entry, dict)
    }


async def stale_approvals(
    session: AsyncSession,
    bot: Bot,
    prefs: AssistantSettings,
    call: feed.Reader,
    now: datetime,
    cfg: tower.Config | None,
) -> int:
    """待批的发消息类审批单：被回复的消息已撤回、所有者已亲自回复、会话又有了新消息的，撤回并入箱告知。

    尽量不读飞书：锚点合成一次 `im.mget`；「所有者已亲自回复」看盯消息记下的发言时间；只有
    游标显示这张单创建之后会话里有新消息，才读一次会话复核（同一个会话只读一次）。
    """
    rows = (
        await session.execute(
            select(
                AssistantApproval.id,
                AssistantApproval.chat_id,
                AssistantApproval.anchor_message_id,
                AssistantApproval.thread_id,
                AssistantApproval.created_at,
            )
            .where(
                AssistantApproval.bot_id == bot.id,
                AssistantApproval.status == "pending",
                # 只看往会话里发新内容的单：改、撤、置顶已有消息的单，锚点是那条消息本身，
                # 「之后又有人发言」不让它过时（执行前复查也不按锚点判断）。
                AssistantApproval.tool.in_(catalog.SENDS),
                AssistantApproval.chat_id.is_not(None),
                AssistantApproval.anchor_message_id.is_not(None),
            )
            .order_by(AssistantApproval.created_at)
            .limit(approvals.MAX_PENDING)
        )
    ).all()
    if not rows:
        return 0
    marks = await _chat_marks(session, bot.id)
    gone = set(await feed.recalled(call, [row.anchor_message_id for row in rows][:CHECK_BATCH]))
    views: dict[tuple[str, str | None], anchors.ChatView] = {}
    withdrawn = 0
    for row in rows:
        created = _ms(row.created_at)
        spoke, latest = marks.get(row.chat_id, (0, 0))
        code: str | None = None
        if row.anchor_message_id in gone:
            code = "anchor_recalled"
        elif spoke > created:
            code = "owner_replied"
        elif latest > created:
            where = (row.chat_id, row.thread_id)
            if where not in views:
                views[where] = await anchors.chat_view(call, row.chat_id, row.thread_id)
            # 平台代发的消息（同会话另一张单批准后发出的）不算所有者亲自回复。
            mine = await sent.among(
                session, bot.id, [item.message_id for item in views[where].messages]
            )
            verdict = anchors.anchor_verdict(
                views[where], row.anchor_message_id, prefs.open_id, mine
            )
            code = _VERDICTS.get(verdict) if verdict else None
        if code is None:
            continue
        done = await approvals.auto_withdraw(session, row.id, code, cfg=cfg)
        if done is None:
            continue
        stored = done.args_enc if isinstance(done.args_enc, dict) else {}
        await add_reminder(
            session,
            bot.id,
            f"approval:{done.id}:withdrawn",
            level="immediate",
            chat_id=done.chat_id,
            now=now,
            summary={
                "reminder": "approval_withdrawn",
                "approval_id": str(done.id),
                "tool": done.tool,
                "code": code,
                "chat_id": done.chat_id,
                "abstract": stored.get("abstract"),
            },
        )
        withdrawn += 1
    return withdrawn


async def recalled_after_delivery(
    session: AsyncSession, bot_id: uuid.UUID, call: feed.Reader, now: datetime
) -> int:
    """最近交回的消息里被对方撤回的：条目作废、关掉它引出的待跟进、入箱告知。返回发现了几条。

    作废之后这条消息不再被查，所以每条只提醒一次。它引出的待批回复由 `stale_approvals` 撤回。
    """
    rows = (
        await session.execute(
            select(AssistantInbox.id, AssistantInbox.message_id, AssistantInbox.chat_id)
            .where(
                AssistantInbox.bot_id == bot_id,
                AssistantInbox.kind == "message",
                AssistantInbox.status == "delivered",
                AssistantInbox.message_id.is_not(None),
                AssistantInbox.updated_at >= now - RECALL_WINDOW,
            )
            .order_by(AssistantInbox.id.desc())
            .limit(CHECK_BATCH)
        )
    ).all()
    if not rows:
        return 0
    gone = set(await feed.recalled(call, [row.message_id for row in rows]))
    found = 0
    for row in rows:
        if row.message_id not in gone:
            continue
        await session.execute(
            update(AssistantInbox)
            .where(AssistantInbox.id == row.id)
            .values(status="void", void_code="recalled", updated_at=func.now())
        )
        closed = await followups.close_for_message(session, bot_id, row.message_id)
        await add_reminder(
            session,
            bot_id,
            f"recalled:{row.message_id}",
            level="batch",
            chat_id=row.chat_id,
            now=now,
            summary={
                "reminder": "message_recalled",
                "chat_id": row.chat_id,
                "message_id": row.message_id,
                "followups": [followups.label(item.seq) for item in closed],
            },
        )
        found += 1
    return found


async def feishu_checks(
    session: AsyncSession, bot: Bot, prefs: AssistantSettings, call: feed.Reader, now: datetime
) -> dict[str, int]:
    """要以所有者身份读飞书的两类检查。读失败只记代码；已经做完的照常由调用方提交。

    意外的异常也在这里收住、只记类型：这一次里可能已经刷新过令牌（旧的刷新令牌已经用掉），
    让它往上抛就会回滚，授权就此作废。会话本身坏了（数据库错误）时调用方的提交会再抛。
    """
    counts = {"assistant_approvals_auto_withdrawn": 0, "assistant_recalls_noticed": 0}
    cfg = tower.config(get_settings())
    try:
        counts["assistant_approvals_auto_withdrawn"] = await stale_approvals(
            session, bot, prefs, call, now, cfg
        )
        counts["assistant_recalls_noticed"] = await recalled_after_delivery(
            session, bot.id, call, now
        )
    except PersonalError as exc:
        log.warning(
            "assistant_delivery_checks_failed", bot_id=str(bot.id), code=sealed.code_only(exc.code)
        )
    except Exception as exc:  # noqa: BLE001 只记类型
        log.warning("assistant_delivery_checks_failed", bot_id=str(bot.id), code=type(exc).__name__)
    return counts


async def remind_reauth(
    session: AsyncSession,
    bot: Bot,
    prefs: AssistantSettings,
    row: AssistantDelivery,
    now: datetime,
) -> int:
    """授权快满一年（或已经满了）：私信所有者一次，每天至多一次。不经模型，不带任何内容。"""
    today = assistant_settings.local_today(prefs, now)
    if row.reauth_reminded_on == today or bot.owner_id is None:
        return 0
    since = await session.scalar(
        select(FeishuPersonalGrant.authorized_at).where(
            FeishuPersonalGrant.bot_id == bot.id,
            FeishuPersonalGrant.user_id == bot.owner_id,
            FeishuPersonalGrant.status == "connected",
        )
    )
    if since is None:
        return 0
    expires = aware_utc(since) + REAUTH_AFTER
    if now < expires - timedelta(days=REAUTH_NOTICE_DAYS):
        return 0
    words = REAUTH_SOON.format(days=(expires - now).days) if now < expires else REAUTH_OVERDUE
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"assistant-reauth:{bot.id}:{today.isoformat()}",
        target={"chat_id": prefs.chat_id},
        payload={"markdown": words},
    )
    row.reauth_reminded_on = today
    return 1


async def sweep(
    session: AsyncSession,
    cipher: Cipher,
    bot: Bot,
    prefs: AssistantSettings,
    row: AssistantDelivery,
    now: datetime,
) -> dict[str, int]:
    """一位助理的一次顺带检查。不提交；调过飞书之后调用方不回滚（刷新的令牌要落库）。"""
    today = assistant_settings.local_today(prefs, now)
    counts = {
        "assistant_followups_due": await followups_due(session, bot.id, today, now),
        "assistant_marks_stale": await stale_marks(session, bot.id, today, now),
        "assistant_reauth_reminded": await remind_reauth(session, bot, prefs, row, now),
        "assistant_approvals_auto_withdrawn": 0,
        "assistant_recalls_noticed": 0,
    }
    call = await reader(session, cipher, bot, prefs)
    if call is not None:
        counts.update(await feishu_checks(session, bot, prefs, call, now))
    return counts


async def _enroll(session: AsyncSession) -> None:
    """给启用中的助理补上投递状态行（新开通的、升级前就有的）。"""
    await session.execute(
        insert(AssistantDelivery)
        .from_select(
            ["bot_id"],
            select(Bot.id).where(
                Bot.kind == "assistant", Bot.enabled.is_(True), Bot.assistant_state == "active"
            ),
        )
        .on_conflict_do_nothing(index_elements=[AssistantDelivery.bot_id])
    )


async def _visit(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, bot_id: uuid.UUID, now: datetime
) -> dict[str, int]:
    """一位助理一个短事务：认领、往后排、过门槛、检查、提交。"""
    async with factory() as session:
        await sealed.guard_logs(session, bot_id)
        row = await delivery.state(session, bot_id, lock=True)
        if row is None:
            return {}
        row.next_sweep_at = now + SWEEP_EVERY
        bot = await session.get(Bot, bot_id)
        prefs = await session.get(AssistantSettings, bot_id)
        counts: dict[str, int] = {}
        # 下班、冷启动没做完、正在停用的助理不检查，但照样往后排。
        if bot is not None and prefs is not None and delivery.gate(bot, prefs, now):
            counts = await sweep(session, cipher, bot, prefs, row, now)
        await session.commit()
        return counts


async def _push_back(
    factory: async_sessionmaker[AsyncSession], bot_id: uuid.UUID, now: datetime
) -> None:
    async with factory() as session:
        await session.execute(
            update(AssistantDelivery)
            .where(AssistantDelivery.bot_id == bot_id)
            .values(next_sweep_at=now + SWEEP_EVERY)
        )
        await session.commit()


async def tick(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, now: datetime
) -> dict[str, int]:
    """scheduler 每 5 秒调一次：认领到点的助理，逐位做顺带检查。一位出错只影响它自己。"""
    async with factory() as session:
        await _enroll(session)
        due = list(
            await session.scalars(
                select(AssistantDelivery.bot_id)
                .join(Bot, Bot.id == AssistantDelivery.bot_id)
                .where(
                    Bot.kind == "assistant",
                    Bot.enabled.is_(True),
                    Bot.assistant_state == "active",
                    AssistantDelivery.next_sweep_at <= now,
                )
                .order_by(AssistantDelivery.next_sweep_at)
                .limit(BATCH)
            )
        )
        await session.commit()
    total: dict[str, int] = {}
    for bot_id in due:
        try:
            # 被叫停时等这一位提交完再退：这一次里可能刚刷新过令牌（见 `watch.finish_round`）。
            counts = await watch.finish_round(_visit(factory, cipher, bot_id, now))
        except Exception as exc:  # noqa: BLE001 一位助理出错不挡别的
            log.warning(
                "assistant_delivery_sweep_failed", bot_id=str(bot_id), error=type(exc).__name__
            )
            await _push_back(factory, bot_id, now)
            continue
        for key, value in counts.items():
            total[key] = total.get(key, 0) + value
    return total
