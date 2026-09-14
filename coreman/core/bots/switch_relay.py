"""切换 relay 的领域逻辑（spec §10.2 权限、§8.5 清会话、§8.8 限流切换复用）。API 与 worker 共用。

只改内存里的对象并写审计 / 通知，**不 commit**：API 端点要在 commit 之后刷 ETag，worker
要把回执与状态清理放进同一个事务，事务边界一律留给调用方。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bots.permissions import can_switch_relay, relay_allowed_for_bot
from coreman.core.bots.relay_policy import relay_visible, validate_model_for_relay
from coreman.core.bots.workspace import reserve_workspace
from coreman.core.chat import sessions
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotSkill, RelayServer, RuntimeNode, User
from coreman.core.errors import ApiError
from coreman.core.knowledge.memory_transfer import transfer
from coreman.core.relay.models import default_model, effective_models, load_catalog, supports_xhigh


class SwitchError(Exception):
    """切换被规则挡下。`status` 直接就是 API 的 HTTP 码，worker 只看它区分 403 与其它。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


@dataclass(frozen=True)
class SwitchResult:
    old_relay_id: uuid.UUID | None
    new_relay_id: uuid.UUID
    old_model: str
    new_model: str
    effort_level: str | None
    memory_status: str = "unchanged"


async def switch_relay(
    session: AsyncSession,
    *,
    bot: Bot,
    target: RelayServer | None,
    model: str | None,
    actor: User,
    member_ids: set[uuid.UUID],
    ip: str | None,
    cipher: Cipher | None = None,
) -> SwitchResult:
    """把机器人换到 `target` 上（可同时指定模型），成功返回换前换后的快照。"""
    if not can_switch_relay(actor, bot, member_ids):
        raise SwitchError(403, "没有权限执行该操作")
    # 换到当前这台不做可见性/团队校验：同 relay 换模型走的也是这个入口，机器人本来就在上面
    # 跑着，PATCH model 同样不看 relay 可见性——对 visibility='admins' 的实例多这一道就成了
    # 「机器人管理员连自己的模型都改不了」。
    same_relay = target is not None and target.id == bot.relay_server_id
    if target is None or not target.is_active or not (same_relay or relay_visible(actor, target)):
        raise SwitchError(422, "目标运行时未注册或不可用")
    creator = await session.get(User, bot.created_by)
    if not same_relay and not relay_allowed_for_bot(
        actor, target, bot_team_id=bot.team_id, creator_team_id=creator.team_id if creator else None
    ):
        raise SwitchError(422, "目标运行时不属于本团队或公共池")
    catalog = await load_catalog(session)
    if model is not None:
        try:
            await validate_model_for_relay(session, target, model, bot.effort_level)
        except ApiError as exc:
            # 领域服务对外只抛 SwitchError：worker 不该认识 API 的异常类型。
            raise SwitchError(exc.status_code, exc.message) from exc
        new_model = model
    elif bot.model in effective_models(target, catalog):
        new_model = bot.model
    else:
        # 跨 provider 换机（claude ↔ codex）时原模型必然落空，退回目标 provider 的默认模型。
        picked = default_model(target.model_provider, catalog)
        if picked is None:
            raise SwitchError(422, "目标运行时没有可用模型")
        new_model = picked
    expected_version = bot.version
    await session.refresh(bot, with_for_update=True)
    if bot.version != expected_version:
        raise SwitchError(409, "机器人已被其他操作修改，请刷新后重试")
    memory_status = "unchanged"
    target_directory = bot.working_dir
    old_runtime = (
        await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    )
    if target.runtime_node_id:
        target_node = await session.get(RuntimeNode, target.runtime_node_id)
        source_node = (
            await session.get(RuntimeNode, old_runtime.runtime_node_id)
            if old_runtime and old_runtime.runtime_node_id
            else None
        )
        if target_node:
            relative = PurePosixPath(bot.bot_key)
            if source_node:
                relative = PurePosixPath(bot.working_dir).relative_to(source_node.workspace_root)
            target_directory = str(PurePosixPath(target_node.workspace_root) / relative)
    if not same_relay:
        try:
            await reserve_workspace(session, target.id, target_directory, bot_id=bot.id)
            if target_directory == bot.working_dir:
                memory_status = await transfer(session, bot, target, cipher)
            else:
                memory_status = await transfer(
                    session, bot, target, cipher, target_directory=target_directory
                )
        except ApiError as exc:
            raise SwitchError(exc.status_code, exc.message) from exc
        installed = await session.scalars(
            select(BotSkill).where(BotSkill.bot_id == bot.id, BotSkill.status == "installed")
        )
        for item in installed:
            item.status = "failed"
            item.error_message = "实例已切换，原授权仍保留，请重新安装以确认目标实例代码。"
    old_relay_id, old_model, old_effort = bot.relay_server_id, bot.model, bot.effort_level
    old_directory = bot.working_dir
    bot.working_dir = target_directory
    bot.relay_server_id, bot.model = target.id, new_model
    if bot.effort_level == "xhigh" and not supports_xhigh(new_model, catalog):
        # 自动换来的模型不支持 xhigh 时降一档，否则下发给 relay 的就是非法档位。
        bot.effort_level = "high"
    # 换机换模型即换上下文：relay_session_id 在新机器上根本不存在，会话一律作废（spec §8.5）。
    await sessions.clear_bot(session, bot.id)
    await record_audit(
        session,
        action="bot.switch_relay",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot",
        target_id=str(bot.id),
        # effort_level 一起进去：自动降档也是这次操作改的，diff_dict 只列变化的键。
        diff=diff_dict(
            {
                "working_dir": old_directory,
                "relay_server_id": str(old_relay_id) if old_relay_id else None,
                "model": old_model,
                "effort_level": old_effort,
            },
            {
                "working_dir": target_directory,
                "relay_server_id": str(target.id),
                "model": new_model,
                "effort_level": bot.effort_level,
            },
        ),
        ip=ip,
    )
    await notify_bot_changed(session, bot.id)
    return SwitchResult(
        old_relay_id, target.id, old_model, new_model, bot.effort_level, memory_status
    )
