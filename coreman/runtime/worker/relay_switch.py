"""relay_switch 任务处理器：切换运行时的后台作业。

两个来源：限流切换卡片（card_actions._ratelimit 只负责在 5 秒内把卡收掉，结论回到聊天），以及
管理台的切换（载荷 origin=admin，管理台轮询任务状态，结论记在任务 result 里）。两者都不要求
员工空闲：先隔离，等手上这一轮结束再迁移。作业中途没了由 reaper 解除隔离（绑定不变）。

任务终态一律 succeeded：切换被规则挡下（权限没了、目标实例被下线）是业务结论，不是可重试
的故障——重试一百次结论也一样，只会把同一句失败回执刷进聊天。结论以回执的形式告诉用户。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.switch_relay import SwitchError, switch_relay
from coreman.core.bots.workspace_transfer import release_fence
from coreman.core.bus import outbox, tasks
from coreman.core.chat import interactions
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import Bot, BotMember, RelayServer, User
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.context import TaskContext


class RelaySwitchHandler:
    """按载荷里的目标实例改绑；卡片来源的把结果回给聊天，管理台来源的写进任务 result。"""

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
            admin = p.get("origin") == "admin"
            outcome: dict[str, Any]
            try:
                if user is None:
                    # 认不出人就当没权限：切换是写操作，宁可让用户去管理台。
                    raise SwitchError(403, msg("relay_switch_forbidden", ctx.locale))
                # 不要求员工空闲：隔离后等已在跑的这一轮结束（用户定：默认等它跑完）。
                result = await switch_relay(
                    session,
                    bot=bot,
                    target=target,
                    model=p.get("model") if admin else None,
                    actor=user,
                    member_ids=member_ids,
                    ip=p.get("ip") if admin else None,
                    cipher=ctx.cipher,
                    workspace_mode=p.get("workspace_mode") or "copy",
                    target_directory=p.get("target_directory") if admin else None,
                    allow_stored_memory=bool(p.get("allow_stored_memory")) and admin,
                    drain=True,
                    cancelled=ctx.cancel_event,
                )
            except SwitchError as exc:
                outcome = {
                    "ok": False,
                    "status": exc.status,
                    "code": exc.code,
                    "message": exc.message,
                }
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
            except Exception:
                # 中途未预期的异常：绑定没改，解除迁移隔离，员工继续在原运行时上工作。
                await session.rollback()
                await release_fence(session, bot.id, "切换中断，原绑定已保留，请重试")
                await session.commit()
                raise
            else:
                outcome = {
                    "ok": True,
                    "old_relay_id": str(result.old_relay_id) if result.old_relay_id else None,
                    "new_relay_id": str(result.new_relay_id),
                    "old_model": result.old_model,
                    "new_model": result.new_model,
                    "memory_status": result.memory_status,
                }
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
            await tasks.finish(
                session, ctx.task.id, status="succeeded", result=outcome, bot_id=ctx.task.bot_id
            )
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
