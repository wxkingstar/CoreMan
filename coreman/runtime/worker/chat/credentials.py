"""个人凭证：本人触发的轮次注入本人在这个 AI 员工里保存的凭证，并下发向本人索取的本轮令牌。

对话轮按已验证的发言者算（私聊、群聊都一样，只给当前发言者自己的）；机器人之间协作、
同事答复后的续接这些轮次不注入；定时任务按执行人算。凭证值进 env，从不进提示词。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, User
from coreman.core.personal_credentials import policy, service, store
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext

API_PATH = "/api/runtime/credentials/requests"
_COLLABORATION_KEYS = ("collaboration_id", "collaboration_phase", "human_collaboration_id")
_NONE: frozenset[str] = frozenset()


def _strip(env: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if not key.startswith(policy.ENV_PREFIX)}


def guidance(names: tuple[str, ...]) -> str:
    have = (
        "当前发言者已在本 AI 员工保存的个人凭证（环境变量，只给名字）："
        + "、".join(f"`${name}`" for name in names)
        + "。"
        if names
        else "当前发言者在本 AI 员工还没有保存个人凭证。"
    )
    return (
        "\n\n## 个人凭证\n"
        + have
        + "\n任务需要当前发言者本人的账号、密码或 API Key 而环境变量里没有，"
        "或者调用时提示凭证无效，就向本人索取：\n"
        "```bash\n"
        'curl -sS -X POST "$COREMAN_CREDENTIAL_URL" '
        '-H "Authorization: Bearer $COREMAN_CREDENTIAL_TOKEN" '
        "-H 'Content-Type: application/json' "
        """-d '{"fields":[{"key":"DEMO_API_KEY","label":"Demo 系统 API Key","secret":true}],"""
        """"purpose":"查询你在 Demo 系统里的订单"}'\n"""
        "```\n"
        "`key` 是环境变量名（大写字母、数字、下划线），`label` 是给用户看的名字，"
        "账号这类非机密字段把 `secret` 设为 false。"
        "调用成功后简短告诉用户「已发送安全表单，请填写」，"
        "然后结束本轮；用户提交后系统会自动让你继续，届时变量已经在环境里。\n"
        "规则：不得让用户在聊天里发送密码或密钥；不得打印、回显或记录这些变量的值，"
        "不得写入文件、工作区或 URL；只用于当前发言者本人的请求；接口地址与令牌只在本轮有效。"
    )


async def _apply(
    session: AsyncSession,
    ctx: TaskContext,
    *,
    bot: Bot,
    user_id: uuid.UUID,
    cap: policy.Capability,
    extra: str,
    env: dict[str, str],
) -> tuple[str, dict[str, str], frozenset[str]]:
    env = _strip(env)
    user = await session.get(User, user_id, populate_existing=True)
    if user is None or user.status != "active" or user.source == "bootstrap":
        return extra, env, _NONE
    found = await store.injected(session, ctx.cipher, bot_id=bot.id, user_id=user_id)
    env.update(found.env)
    env[policy.ENV_PREFIX + "URL"] = ctx.public_base_url.rstrip("/") + API_PATH
    env[policy.ENV_PREFIX + "TOKEN"] = policy.issue_capability(
        ctx.cipher, cap, ttl_seconds=bot.sse_timeout_seconds + policy.CAPABILITY_GRACE
    )
    return extra + guidance(found.names), env, found.secret_values


async def configure(
    session: AsyncSession, ctx: TaskContext, intake: Intake, extra: str, env: dict[str, str]
) -> tuple[str, dict[str, str], frozenset[str]]:
    payload = ctx.task.payload
    user_id = intake.speaker.user_id
    if (
        intake.bot.platform not in service.PLATFORMS
        or user_id is None
        or any(payload.get(key) for key in _COLLABORATION_KEYS)
    ):
        return extra, _strip(env), _NONE
    cap = policy.Capability(
        task_id=ctx.task.id,
        bot_id=intake.bot.id,
        user_id=user_id,
        origin_kind="chat",
        chat_id=intake.chat_id,
        chat_type=intake.chat_type,
        session_key=intake.session_key,
        event_id=intake.inbound.id,
        cron_job_id=None,
    )
    return await _apply(
        session, ctx, bot=intake.bot, user_id=user_id, cap=cap, extra=extra, env=env
    )


async def configure_cron(
    session: AsyncSession,
    ctx: TaskContext,
    *,
    bot: Bot,
    actor_id: uuid.UUID,
    job_id: uuid.UUID,
    extra: str,
    env: dict[str, str],
) -> tuple[str, dict[str, str], frozenset[str]]:
    if bot.platform not in service.PLATFORMS:
        return extra, _strip(env), _NONE
    cap = policy.Capability(
        task_id=ctx.task.id,
        bot_id=bot.id,
        user_id=actor_id,
        origin_kind="cron",
        chat_id=f"cron:{job_id}",
        chat_type="cron",
        session_key=None,
        event_id=None,
        cron_job_id=job_id,
    )
    return await _apply(session, ctx, bot=bot, user_id=actor_id, cap=cap, extra=extra, env=env)
