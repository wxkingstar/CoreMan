"""个人凭证：本人触发的轮次注入本人在这个 AI 员工里保存的凭证，并下发向本人索取的本轮令牌。

对话轮按已验证的发言者算（私聊、群聊都一样，只给当前发言者自己的）；机器人之间协作、
同事答复后的续接这些轮次不注入；定时任务按执行人算。一次性交付的值只注入它自己的续接轮。
凭证值进 env，从不进提示词。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, CredentialRequest, User
from coreman.core.personal_credentials import policy, service, store
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext

API_PATH = "/api/runtime/credentials/requests"
_COLLABORATION_KEYS = ("collaboration_id", "collaboration_phase", "human_collaboration_id")
_NONE: frozenset[str] = frozenset()


def _strip(env: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if not key.startswith(policy.ENV_PREFIX)}


def _names(names: tuple[str, ...]) -> str:
    return "、".join(f"`${name}`" for name in names)


def status(
    names: tuple[str, ...],
    *,
    once: tuple[str, ...] = (),
    once_lost: bool = False,
) -> str:
    """本轮块里的个人凭证状态。

    `once`：本轮注入的一次性密钥；`once_lost`：这是一次性交付的续接轮，但值已经不在了。
    """
    have = (
        "- 个人凭证：当前发言者已在本 AI 员工保存（环境变量，只给名字）" + _names(names) + "。"
        if names
        else "- 个人凭证：当前发言者在本 AI 员工还没有保存个人凭证。"
    )
    if once:
        have += (
            "本轮有用户刚提交的一次性密钥：" + _names(once) + "。只在本轮的环境里，"
            "本轮结束即删除，下一轮不会再有；本轮没能用上就重新索取。"
        )
    elif once_lost:
        have += "用户提交的一次性密钥已不可用（例如等待续接太久已被删除）；仍然需要就重新索取。"
    return have


# 发言者身份未知、或账号不可用（停用、引导管理员）时的本轮状态：规则照挂，稳定段不随人变。
UNAVAILABLE = "- 个人凭证：本轮发言者身份未验证或账号不可用，不能索取或使用个人凭证。"


def guidance(origin_kind: str) -> str:
    """个人凭证的索取方法与规则（system prompt）；已保存哪些、本轮有没有一次性密钥见 `status`。"""
    if origin_kind == "cron":
        # 定时任务没有会话可续：提交后不会再唤醒本轮，新值要等下一次定时运行。
        keep = "定时任务里 `save` 必须为 true：一次性密钥没有续接轮可用。\n"
        after = (
            "调用成功后在输出里说明本次缺少凭证、已向用户发送安全表单，然后结束本轮；"
            "这是定时任务，用户提交后不会续接本轮，新值从下一次定时运行起生效。\n"
        )
    else:
        keep = (
            "`save` 决定值要不要留下：\n"
            "- `true`：本人自己的账号、密码、API Key，以后还要代他使用。"
            "保存后本人之后的每一轮都会注入，本人可以在「我的凭证」里查看和删除。\n"
            "- `false`（默认）：一次性密钥，例如要你写进服务器、配置文件、密钥库或 CI 变量的值。"
            "只注入提交后续接的那一轮，那一轮结束即删除，不会保存。拿不准时用 false。\n"
        )
        after = (
            "调用成功后简短告诉用户「已发送安全表单，请填写」，"
            "然后结束本轮；用户提交后系统会自动让你继续，届时变量已经在环境里。\n"
        )
    return (
        "\n\n## 个人凭证\n"
        "已保存的个人凭证会以环境变量注入当前发言者本人触发的轮次，变量名见本轮块。"
        "\n任务需要当前发言者本人提供账号、密码或密钥而环境变量里没有，"
        "或者调用时提示凭证无效，就向本人索取：\n"
        "```bash\n"
        'curl -sS -X POST "$COREMAN_CREDENTIAL_URL" '
        '-H "Authorization: Bearer $COREMAN_CREDENTIAL_TOKEN" '
        "-H 'Content-Type: application/json' "
        """-d '{"fields":[{"key":"DEMO_API_KEY","label":"Demo 系统 API Key","secret":true}],"""
        """"purpose":"查询你在 Demo 系统里的订单","save":true}'\n"""
        "```\n"
        "`key` 是环境变量名（大写字母、数字、下划线），`label` 是给用户看的名字，"
        "账号这类非机密字段把 `secret` 设为 false。"
        + keep
        + after
        + "规则：不得让用户在聊天里发送密码或密钥；不得打印、回显或记录这些变量的值；"
        "只用于当前发言者本人的请求；接口地址与令牌只在本轮有效。"
        "保存的个人凭证不得写入文件、工作区或 URL。"
        "一次性密钥只写进用户要求的那个位置：用变量引用写入"
        "（例如 `yq -i '.values.X = strenv(X)' 文件`），不要把值写成命令里的字面量；"
        "核对时只看键是否存在或比对哈希，不要 `cat`、`git diff`、`git show` 含值的内容；"
        "不要另存副本。"
    )


async def _handoff(
    session: AsyncSession, ctx: TaskContext, intake: Intake
) -> CredentialRequest | None:
    """本轮是一次性交付的续接轮时，返回那条请求；其余轮次一律没有。"""
    raw_id = ctx.task.payload.get("credential_request_id")
    if ctx.task.kind != service.RESUME_KIND or not raw_id:
        return None
    row = await session.get(CredentialRequest, uuid.UUID(str(raw_id)), populate_existing=True)
    if (
        row is None
        or row.save
        or row.resume_task_id != ctx.task.id
        or row.bot_id != intake.bot.id
        or row.user_id != intake.speaker.user_id
    ):
        return None
    return row


async def _apply(
    session: AsyncSession,
    ctx: TaskContext,
    *,
    bot: Bot,
    user_id: uuid.UUID,
    cap: policy.Capability,
    extra: str,
    env: dict[str, str],
    handoff: CredentialRequest | None = None,
) -> tuple[str, dict[str, str], frozenset[str]]:
    env = _strip(env)
    extra += guidance(cap.origin_kind)
    user = await session.get(User, user_id, populate_existing=True)
    if user is None or user.status != "active" or user.source == "bootstrap":
        ctx.turn_notes.append(UNAVAILABLE)
        return extra, env, _NONE
    found = await store.injected(session, ctx.cipher, bot_id=bot.id, user_id=user_id)
    env.update(found.env)
    # 一次性交付压过同名的个人凭证：用户这一次给的就是这一次要用的。
    once = (
        store.handed_off(ctx.cipher, handoff)
        if handoff is not None
        else store.Injected({}, _NONE, ())
    )
    env.update(once.env)
    env[policy.ENV_PREFIX + "URL"] = ctx.public_base_url.rstrip("/") + API_PATH
    env[policy.ENV_PREFIX + "TOKEN"] = policy.issue_capability(
        ctx.cipher, cap, ttl_seconds=bot.sse_timeout_seconds + policy.CAPABILITY_GRACE
    )
    ctx.turn_notes.append(
        status(
            tuple(name for name in found.names if name not in once.env),
            once=once.names,
            once_lost=handoff is not None and not once.names,
        )
    )
    return extra, env, found.secret_values | once.secret_values


async def configure(
    session: AsyncSession,
    ctx: TaskContext,
    intake: Intake,
    extra: str,
    env: dict[str, str],
    *,
    relay_session_id: uuid.UUID,
) -> tuple[str, dict[str, str], frozenset[str]]:
    payload = ctx.task.payload
    user_id = intake.speaker.user_id
    if intake.bot.platform not in service.PLATFORMS or any(
        payload.get(key) for key in _COLLABORATION_KEYS
    ):
        return extra, _strip(env), _NONE
    if user_id is None:
        # 群里身份未知的人说话：规则照挂，system prompt 不因换人而变；本轮块说明用不了。
        ctx.turn_notes.append(UNAVAILABLE)
        return extra + guidance("chat"), _strip(env), _NONE
    cap = policy.Capability(
        task_id=ctx.task.id,
        bot_id=intake.bot.id,
        user_id=user_id,
        origin_kind="chat",
        chat_id=intake.chat_id,
        chat_type=intake.chat_type,
        session_key=intake.session_key,
        relay_session_id=relay_session_id,
        event_id=intake.inbound.id,
        cron_job_id=None,
    )
    return await _apply(
        session,
        ctx,
        bot=intake.bot,
        user_id=user_id,
        cap=cap,
        extra=extra,
        env=env,
        handoff=await _handoff(session, ctx, intake),
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
        relay_session_id=None,
        event_id=None,
        cron_job_id=job_id,
    )
    return await _apply(session, ctx, bot=bot, user_id=actor_id, cap=cap, extra=extra, env=env)
