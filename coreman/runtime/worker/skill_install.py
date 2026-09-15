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
from coreman.core.relay.agent_client import call_agent
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext


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
    if relay is None or not relay.is_active or not relay.agent_token_enc:
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
        work = asyncio.create_task(self.install(ctx))
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
            await self.fail(ctx, cancelled=True)
            if not ctx.cancel_event.is_set():
                raise
        except Exception:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            # Agent 错误可能含第三方输出；仅返回固定文案。
            await self.fail(ctx)
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

    async def install(self, ctx: TaskContext) -> None:
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

    async def fail(self, ctx: TaskContext, *, cancelled: bool = False) -> None:
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
                error_message="安装未完成，请检查实例状态后重新提交；远端操作可能已执行。",
            )
            if row and row.install_task_id == ctx.task.id:
                row.status, row.install_task_id = "failed", None
                row.error_message = "安装未完成，请检查实例状态后重新提交；远端操作可能已执行。"
            await session.commit()
