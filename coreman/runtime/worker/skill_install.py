"""安装任务：外部操作前后校验授权，失联结果不自动重试。"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.events import notify_bot_changed
from coreman.core.bots.secrets import decrypt_json, encrypt_json
from coreman.core.bus import tasks
from coreman.core.db.models import (
    Bot,
    BotSkill,
    EnvPreset,
    RelayServer,
    Skill,
    SkillApproval,
    SkillSource,
    Task,
    User,
)
from coreman.core.knowledge import installation as installs
from coreman.core.knowledge.git_auth import SOURCE_TOKEN_AAD, https_repository
from coreman.core.knowledge.skill_policy import env_vars, selected_groups, user_inputs
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext

INCOMPLETE = "安装未完成，请检查实例状态后重新提交；远端操作可能已执行。"
CANCELLED = "安装已取消；远端操作可能已执行，请检查实例状态后按需重新提交。"

# 安装前后校验失败的错误码 → 用户可见原因。
REASONS = {
    "installation_no_longer_active": "安装任务已取消或已结束",
    "installation_configuration_changed": "技能已停用或修改，或安装记录已变化",
    "installation_target_changed": "技能来源、机器人所属实例或工作目录在提交后发生了变化",
    "installation_permission_revoked": "提交人已不是该机器人的管理员，或执行人账号已停用",
    "installation_approval_invalid": "内部技能审批已失效，或与本次安装的数据库、安全提示不一致",
    "installation_actor_mismatch": "执行人与提交人不一致",
    "installation_agent_unavailable": "机器人所属实例已停用或未绑定运行时节点",
    "installation_directory_shared": "同一实例上另有机器人使用相同工作目录，安装会影响对方",
    "installation_preset_missing": "所选环境变量预设已被删除",
    "installation_preset_conflict": "所选环境变量预设中有同名变量但取值不同",
    "installation_environment_conflict": "与该机器人已安装技能的环境变量同名但取值不同",
    "installation_mcp_missing": "MCP 技能缺少 MCP 配置",
    "installation_source_missing": "技能及其来源都没有配置 Git 地址",
    "installation_token_repository_mismatch": "技能仓库与来源仓库不一致，不能使用来源的访问令牌",
    "installation_configuration_changed_during_execution": "安装期间实例或环境变量配置发生了变化",
}

# 节点固定提示（前缀）→ 处理建议；退出码等后缀可变。
HINTS = (
    (
        "Git 来源不在白名单内",
        # 新版节点安装技能不再检查白名单，只有未升级的节点还会这样拒绝。
        "该节点版本较旧，安装技能仍受 Git 白名单限制：请升级运行时节点，"
        "或在节点 config.json 的 git_hosts 中加入技能仓库域名后重启服务。",
    ),
    ("工作目录不存在", "机器人的工作目录尚未在运行时节点上创建，请先与机器人对话一次后重试。"),
    ("操作超时", "请检查运行时节点到 Git 仓库与 npm 源的网络后重试。"),
    ("操作失败（退出码", "Git 或 npx 命令失败，请在运行时节点上检查仓库权限、网络与 npx 后重试。"),
)


def failure_reason(exc: BaseException, *, remote: bool) -> tuple[str, str]:
    """返回（日志用错误码，用户可见原因）。

    原因只由平台固定文案和节点 operation_failed 的固定提示组成：Agent 的其它错误文本
    可能含第三方输出，不写入库。
    """
    if isinstance(exc, ValueError) and str(exc) in REASONS:
        code = str(exc)
        if remote:
            return code, f"安装未完成：{REASONS[code]}；远端操作已执行，请检查实例状态后重新提交。"
        return code, f"安装未执行：{REASONS[code]}，请处理后重新提交。"
    if isinstance(exc, AgentError):
        code = exc.code or "agent_error"
        if exc.detail:
            hint = next((h for p, h in HINTS if exc.detail.startswith(p)), "处理后请重新提交。")
            return code, f"运行时节点报告安装失败：{exc.detail}。{hint}"
        if exc.code == "execution_failed":
            return code, (
                "运行时节点执行安装时出错，未返回具体原因（节点版本较旧或出现意外错误）；"
                "请查看节点 runtime.log，或升级运行时后重新提交。远端操作可能已执行。"
            )
        return code, INCOMPLETE
    if isinstance(exc, TimeoutError):
        return "timeout", (
            "安装超过 16 分钟未完成，已停止等待；请检查实例状态后重新提交，远端操作可能已执行。"
        )
    return type(exc).__name__, INCOMPLETE


async def checked(
    session: AsyncSession, ctx: TaskContext
) -> tuple[Bot, Skill, BotSkill, RelayServer, dict[str, Any], SkillApproval | None, dict[str, str]]:
    bot = await session.scalar(select(Bot).where(Bot.id == ctx.task.bot_id).with_for_update())
    task = await session.scalar(select(Task).where(Task.id == ctx.task.id).with_for_update())
    if bot is None or task is None or task.status not in tasks.ACTIVE or task.cancel_requested_at:
        raise ValueError("installation_no_longer_active")
    skill = await session.get(Skill, uuid.UUID(task.payload["skill_id"]))
    row = await session.get(BotSkill, (bot.id, skill.id)) if skill else None
    inputs = json.loads(ctx.cipher.decrypt(task.payload["inputs_enc"], installs.INPUTS_AAD))
    if (
        skill is None
        or not skill.enabled
        or skill.revision != task.payload["skill_revision"]
        or row is None
        or row.status != "installing"
        or row.install_task_id != task.id
    ):
        raise ValueError("installation_configuration_changed")
    source = await session.get(SkillSource, skill.source_id)
    if (
        source is None
        or str(source.id) != inputs["source_id"]
        or source.version != inputs["source_version"]
        or str(bot.relay_server_id) != inputs["relay_id"]
        or bot.working_dir != inputs["working_dir"]
    ):
        raise ValueError("installation_target_changed")
    requester = await session.get(User, uuid.UUID(inputs["requester_id"]))
    actor = await session.get(User, task.user_id) if task.user_id else None
    if (
        requester is None
        or not await installs.bot_admin(session, bot, requester)
        or actor is None
        or actor.status != "active"
    ):
        raise ValueError("installation_permission_revoked")
    approval = None
    if skill.security_level == "internal":
        approval_id = task.payload.get("approval_id")
        approval = await session.get(SkillApproval, uuid.UUID(approval_id)) if approval_id else None
        if (
            approval is None
            or approval.status != "approved"
            or approval.bot_id != bot.id
            or approval.skill_id != skill.id
            or approval.reviewed_by != actor.id
            or actor.role not in ("ai_committee", "platform_admin")
            or approval.skill_revision != skill.revision
            or set(inputs["selected_env_groups"]) != set(approval.approved_databases or [])
            or inputs["security_prompt"] != approval.approved_security_prompt
        ):
            raise ValueError("installation_approval_invalid")
    elif actor.id != requester.id:
        raise ValueError("installation_actor_mismatch")
    relay = await session.get(RelayServer, bot.relay_server_id)
    # 实例都由运行时节点提供，Agent 走节点反向通道，不再要求单独配置的 Agent 端口。
    if relay is None or not relay.is_active or relay.runtime_node_id is None:
        raise ValueError("installation_agent_unavailable")
    # 工作目录共享会让一个机器人的安装改写另一个机器人的运行环境。
    others = await session.scalar(
        select(Bot.id)
        .where(
            Bot.id != bot.id, Bot.relay_server_id == relay.id, Bot.working_dir == bot.working_dir
        )
        .limit(1)
    )
    if others:
        raise ValueError("installation_directory_shared")
    values: dict[str, str] = {}
    for key in selected_groups(skill, inputs["selected_env_groups"], inputs["data_source"]):
        preset = await session.get(EnvPreset, key)
        if preset is None:
            raise ValueError("installation_preset_missing")
        for name, value in env_vars(
            decrypt_json(ctx.cipher, preset.vars_enc, installs.PRESET_AAD)
        ).items():
            if name in values and values[name] != value:
                raise ValueError("installation_preset_conflict")
            values[name] = value
    values.update(user_inputs(skill, inputs["user_env_vars"]))
    others_rows = await session.scalars(
        select(BotSkill).where(
            BotSkill.bot_id == bot.id,
            BotSkill.skill_id != skill.id,
            BotSkill.status != "uninstalled",
            BotSkill.installed_at.is_not(None),
        )
    )
    for other in others_rows:
        if other.effective_env_enc:
            for name, value in decrypt_json(
                ctx.cipher, other.effective_env_enc, installs.EFFECTIVE_AAD
            ).items():
                if name in values and values[name] != value:
                    raise ValueError("installation_environment_conflict")
    return bot, skill, row, relay, inputs, approval, values


class SkillInstallHandler:
    kind = "skill_install"

    async def run(self, ctx: TaskContext) -> None:
        # 是否已向节点下发安装：之后的校验失败意味着远端操作已经执行。
        progress = {"remote": False}
        work = asyncio.create_task(self.install(ctx, progress))
        stopping = asyncio.create_task(ctx.cancel_event.wait())
        try:
            async with asyncio.timeout(960):
                ready, _ = await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stopping in ready:
                    raise asyncio.CancelledError()
                await work
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            ctx.log.info("skill_install_cancelled", skill_id=ctx.task.payload.get("skill_id"))
            await self.fail(ctx, CANCELLED, cancelled=True)
            if not ctx.cancel_event.is_set():
                raise
        except Exception as exc:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            code, reason = failure_reason(exc, remote=progress["remote"])
            ctx.log.warning(
                "skill_install_failed",
                skill_id=ctx.task.payload.get("skill_id"),
                error_code=code,
                error_type=type(exc).__name__,
                detail=exc.detail if isinstance(exc, AgentError) else None,
                remote=progress["remote"],
            )
            await self.fail(ctx, reason)
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

    async def install(self, ctx: TaskContext, progress: dict[str, bool]) -> None:
        async with ctx.session_factory() as session:
            bot, skill, row, relay, inputs, approval, values = await checked(session, ctx)
            source = await session.get(SkillSource, skill.source_id)
            assert source is not None
            payload: dict[str, Any] = {
                "project_dir": bot.working_dir,
                "install_type": skill.install_type,
                "skill_name": skill.name,
            }
            if skill.install_type == "mcp":
                if not skill.mcp_config_enc:
                    raise ValueError("installation_mcp_missing")
                payload["mcp_config"] = json.loads(
                    ctx.cipher.decrypt(skill.mcp_config_enc, installs.MCP_AAD)
                )
            else:
                url = skill.external_repo_url or source.git_url
                if not url:
                    raise ValueError("installation_source_missing")
                payload["git_url"] = url
                # Never forward a source credential to an override repository.
                if source.access_token_enc:
                    if not source.git_url or https_repository(url) != https_repository(
                        source.git_url
                    ):
                        raise ValueError("installation_token_repository_mismatch")
                    payload["git_url"] = https_repository(url)
                    payload["git_access_token"] = ctx.cipher.decrypt(
                        source.access_token_enc, SOURCE_TOKEN_AAD
                    )
            needs_code = row.installed_at is None or inputs["reinstall_code"]
            relay_version = relay.version
            await session.commit()
        if needs_code:
            progress["remote"] = True
            await call_agent(relay, ctx.cipher, "install-skill", payload)
        async with ctx.session_factory() as session:
            bot, skill, row, relay, inputs, approval, current_values = await checked(session, ctx)
            if relay.version != relay_version or current_values != values:
                raise ValueError("installation_configuration_changed_during_execution")
            row.status, row.install_task_id, row.error_message = "installed", None, None
            if needs_code:
                row.version, row.installed_at = skill.version, utcnow()
            row.selected_env_groups = inputs["selected_env_groups"]
            row.user_env_vars_enc = encrypt_json(
                ctx.cipher, inputs["user_env_vars"], installs.USER_ENV_AAD
            )
            row.effective_env_enc = encrypt_json(ctx.cipher, values, installs.EFFECTIVE_AAD)
            row.security_prompt = inputs["security_prompt"]
            row.approved_databases = inputs["approved_databases"]
            row.approved_by = approval.reviewed_by if approval else None
            row.approved_at = approval.reviewed_at if approval else None
            await session.flush()
            await installs.rebuild_prompt(session, bot)
            await notify_bot_changed(session, bot.id)
            await tasks.finish(
                session,
                ctx.task.id,
                status="succeeded",
                only_active=True,
                result={"skill_id": str(skill.id)},
            )
            await session.commit()

    async def fail(self, ctx: TaskContext, reason: str, *, cancelled: bool = False) -> None:
        async with ctx.session_factory() as session:
            await session.scalar(select(Bot).where(Bot.id == ctx.task.bot_id).with_for_update())
            row = await session.get(
                BotSkill, (ctx.task.bot_id, uuid.UUID(ctx.task.payload["skill_id"]))
            )
            await tasks.finish(
                session,
                ctx.task.id,
                status="cancelled" if cancelled else "failed",
                only_active=True,
                error_code="skill_install_incomplete",
                error_message=reason,
            )
            if row and row.install_task_id == ctx.task.id:
                row.status, row.install_task_id = "failed", None
                row.error_message = reason
            await session.commit()
