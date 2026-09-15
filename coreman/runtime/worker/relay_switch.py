"""relay_switch 任务处理器：执行限流切换卡片上的选择（spec §8.8）。

卡片那一轮（card_actions._ratelimit）只负责在 5 秒内把卡收掉，真正的改绑排到这里：换机要
清会话、写审计、发 pg_notify，都不是卡片回调窗口里做得完的事。

任务终态一律 succeeded：切换被规则挡下（权限没了、目标实例被下线）是业务结论，不是可重试
的故障——重试一百次结论也一样，只会把同一句失败回执刷进聊天。结论以回执的形式告诉用户。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.switch_relay import SwitchError, switch_relay
from coreman.core.bus import outbox, tasks
from coreman.core.chat import interactions
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import Bot, BotMember, RelayServer, User
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.context import TaskContext


class RelaySwitchHandler:
    """限流切换（快车道）：按卡片状态里记下的目标实例改绑，并把结果回给聊天。"""

    kind = "relay_switch"

    async def run(self, ctx: TaskContext) -> None:
        p: dict[str, Any] = dict(ctx.task.payload or {})
        current, target_name = str(p.get("current_name") or ""), str(p.get("target_name") or "")
        chat_id = str(p.get("chat_id") or "")
        async with ctx.session_factory() as session:
            bot = await session.get(Bot, ctx.task.bot_id)
            if bot is None:
                await tasks.finish(
                    session, ctx.task.id, status="cancelled", error_code="bot_missing"
                )
                await session.commit()
                return
            user = await self._actor(session, bot, p, ctx)
            target = (
                await session.get(RelayServer, uuid.UUID(str(p["target_relay_server_id"])))
                if p.get("target_relay_server_id")
                else None
            )
            member_ids = set(
                (await session.execute(select(BotMember.user_id).where(BotMember.bot_id == bot.id)))
                .scalars()
                .all()
            )
            try:
                if user is None:
                    # 认不出人就当没权限：切换是写操作，宁可让用户去管理台。
                    raise SwitchError(403, msg("relay_switch_forbidden", ctx.locale))
                result = await switch_relay(
                    session,
                    bot=bot,
                    target=target,
                    model=None,
                    actor=user,
                    member_ids=member_ids,
                    ip=None,
                    cipher=ctx.cipher,
                )
            except SwitchError as exc:
                detail = (
                    msg("relay_switch_forbidden", ctx.locale) if exc.status == 403 else exc.message
                )
                text = msg(
                    "relay_switch_failed",
                    ctx.locale,
                    current=current,
                    target=target_name,
                    detail=detail,
                )
                ctx.log.info("relay_switch_failed", status=exc.status)
            else:
                text = msg(
                    "relay_switch_ok",
                    ctx.locale,
                    current=current,
                    target=target_name,
                    detail=msg("relay_switch_detail", ctx.locale, model=result.new_model),
                )
                ctx.log.info(
                    "relay_switch_done", new_relay=str(result.new_relay_id), model=result.new_model
                )
            if chat_id:
                await outbox.add(
                    session,
                    bot_id=bot.id,
                    platform=bot.platform,
                    kind="send",
                    dedupe_key=f"{ctx.task.id}:send:switch",
                    target={"chat_id": chat_id},
                    payload={"markdown": text},
                )
            if p.get("state_id"):
                # 这轮卡片到此为止：状态留着只会让用户再点一次旧卡又排一个切换任务。
                await interactions.remove(session, uuid.UUID(str(p["state_id"])))
            await tasks.finish(session, ctx.task.id, status="succeeded")
            await session.commit()

    async def _actor(
        self, session: AsyncSession, bot: Bot, p: dict[str, Any], ctx: TaskContext
    ) -> User | None:
        """执行人：优先用卡片状态里记下的 user_id，没有就按平台 id 现解析一次。"""
        if p.get("user_id"):
            return await session.get(User, uuid.UUID(str(p["user_id"])))
        speaker = await resolve_speaker(
            session,
            platform=bot.platform,
            platform_user_id=str(p.get("platform_user_id") or ""),
            resolver=ctx.openuserid,
        )
        return await session.get(User, speaker.user_id) if speaker.user_id else None
