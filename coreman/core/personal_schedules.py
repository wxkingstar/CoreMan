"""本人专属的 AI 定时任务：模型只拟草稿，本人在私聊里点卡片确认后才创建或修改。

任务归本人所有、以本人身份运行，所以不需要机器人管理员权限；代价（每次执行都调模型）用数量
上限和最短间隔约束。结果默认只发回本人与机器人的私聊，也可以由本人选择再发给：私聊过这个
机器人的同事、本人在里面和机器人说过话的群。只有结果只发本人时，任务运行才能以本人身份用
飞书或企业微信个人工具（见 feishu_personal.policy.self_only）。

身份只来自已验证的私聊来源（飞书与企业微信各自的 policy.task_scope），从不取自调用方参数。
新建、修改、重新启用都要本人点确认卡片：模型可能被聊天或工具结果里的内容诱导，而任务以后会
以本人身份运行。暂停、删除、立即运行、查看不改变「以谁的身份跑什么」，直接执行。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.chat import interactions
from coreman.core.cron.precheck import PrecheckError, validate_script
from coreman.core.cron.schedule import next_run
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    CronJob,
    InboundEvent,
    InteractionState,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.errors import ApiError
from coreman.core.prompting import Speaker
from coreman.core.reminders import MODE as REMINDER_MODE
from coreman.core.timeutils import utcnow

MODE = "personal_ai"
KIND = "personal_schedule"
PLATFORMS = ("feishu", "wecom")
TIMEZONE = "Asia/Shanghai"
MAX_ACTIVE = 10
MAX_PENDING = 5
MIN_INTERVAL = timedelta(hours=1)
ONCE_MIN_LEAD = timedelta(minutes=1)
ONCE_MAX_LEAD = timedelta(days=30)
PROPOSAL_TTL = timedelta(minutes=30)
NAME_MAX, PROMPT_MAX = 40, 8000
PRECHECK_MAX = 32768
# 除本人以外最多几位同事、几个群。
MAX_RECIPIENTS, MAX_CHATS = 20, 10
# 检查最短间隔时往后看多少次触发；一年以外的触发不再看。
INTERVAL_SAMPLES = 60
CARD_EVENT = "personal_schedule"
# 企业微信确认卡片的 task_id：`wecom_schedule@<来源任务>@<草稿>`，不超过 128 字节。
WECOM_CARD_PREFIX = "wecom_schedule"
ACTION_TITLES = {"create": "创建", "update": "修改", "resume": "启用"}
SELF_LABEL = "我（私聊）"


class ScheduleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Origin(Protocol):
    """已核验的本人私聊；飞书与企业微信 policy 的 Scope 都满足。"""

    @property
    def task(self) -> Task: ...
    @property
    def bot(self) -> Bot: ...
    @property
    def user_id(self) -> uuid.UUID: ...
    @property
    def chat_id(self) -> str: ...
    @property
    def scheduled(self) -> bool: ...


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]
Instruction = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PROMPT_MAX)
]
Cron = Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)]
When = Annotated[str, StringConstraints(max_length=40)]
ChatId = Annotated[str, StringConstraints(min_length=1, max_length=254, pattern=r"^[^\r\n\x00]+$")]


class Propose(_Arguments):
    name: Name = Field(description="Short task name shown to the user")
    prompt: Instruction = Field(
        description="Complete, self-contained instruction executed on every run"
    )
    cron_expression: Cron | None = Field(
        default=None,
        description="Recurring: five-field cron (minute hour day month weekday) in Beijing time",
    )
    run_at: When | None = Field(
        default=None,
        description="One-off: ISO 8601 time with timezone offset, e.g. 2026-01-02T09:00:00+08:00",
    )

    @model_validator(mode="after")
    def one_schedule(self) -> Propose:
        if (self.cron_expression is None) == (self.run_at is None):
            raise ValueError("exactly one of cron_expression and run_at is required")
        return self


class Draft(Propose):
    """技能接口的完整草稿：在 `Propose` 之上加接收人、执行前检查与到期时间。"""

    expires_at: When | None = None
    include_self: bool = True
    recipient_user_ids: list[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS)
    recipient_chat_ids: list[ChatId] = Field(default_factory=list, max_length=MAX_CHATS)
    precheck_script: str | None = Field(default=None, max_length=PRECHECK_MAX)


TOOLS: dict[str, tuple[type[_Arguments], str]] = {
    "schedule_propose": (
        Propose,
        "Draft a scheduled AI task owned by this user; results go only to this private chat. "
        "The system sends a confirmation card and only the user's click creates it, so never "
        "claim it has been created. Limits: 10 active tasks per user, recurring runs at least "
        "one hour apart, one-off runs between 1 minute and 30 days from now.",
    ),
    "schedule_list": (_Arguments, "List this user's scheduled tasks and reminders here."),
}


def definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": description, "inputSchema": schema.model_json_schema()}
        for name, (schema, description) in TOOLS.items()
    ]


@dataclass(frozen=True)
class Plan:
    schedule_kind: str
    cron_expression: str
    run_at: datetime | None
    next_runs: list[datetime]


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleError("invalid_schedule", f"{field} 需要带时区的 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScheduleError("invalid_schedule", f"{field} 需要带时区的 ISO 8601 时间")
    return parsed.astimezone(UTC)


def plan(cron_expression: str | None, run_at: str | datetime | None, now: datetime) -> Plan:
    """校验执行时间并给出接下来几次触发；不合规抛 `ScheduleError`。"""
    if cron_expression:
        runs: list[datetime] = []
        after = now
        for _ in range(INTERVAL_SAMPLES):
            try:
                upcoming = next_run(cron_expression, TIMEZONE, after)
            except ValueError as exc:
                raise ScheduleError("invalid_schedule", str(exc)) from exc
            if runs and upcoming - runs[-1] < MIN_INTERVAL:
                raise ScheduleError("interval_too_short", "两次执行至少间隔 1 小时")
            runs.append(upcoming)
            if upcoming - now > timedelta(days=366):
                break
            after = upcoming
        return Plan("recurring", cron_expression, None, runs[:3])
    if isinstance(run_at, str):
        run_at = _aware(run_at, "run_at")
    if run_at is None:
        raise ScheduleError("invalid_schedule", "需要 cron_expression 或 run_at")
    at = run_at.astimezone(UTC)
    if not now + ONCE_MIN_LEAD <= at <= now + ONCE_MAX_LEAD:
        raise ScheduleError("run_at_out_of_range", "一次性任务的时间需要在 1 分钟之后、30 天之内")
    return Plan("once", "", at, [at])


def expiry(value: str | None, result: Plan, now: datetime) -> datetime | None:
    if not value:
        return None
    at = _aware(value, "expires_at")
    if at <= now or (result.run_at is not None and at <= result.run_at):
        raise ScheduleError("invalid_expiry", "到期时间需要晚于现在，一次性任务还要晚于执行时间")
    return at


def check_precheck(script: str | None) -> str | None:
    if not script or not script.strip():
        return None
    try:
        validate_script(script)
    except PrecheckError as exc:
        raise ScheduleError("invalid_precheck", f"执行前检查脚本不可用：{exc}") from exc
    return script


def shown(at: datetime | None) -> str:
    if at is None:
        return "—"
    return at.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")


def describe(schedule_kind: str, cron_expression: str, run_at: datetime | None) -> str:
    if schedule_kind == "once":
        return f"一次性，{shown(run_at)}（北京时间）"
    return f"周期，cron `{cron_expression}`（北京时间）"


async def active_count(session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(CronJob)
            .where(
                CronJob.bot_id == bot_id,
                CronJob.created_by == user_id,
                CronJob.execution_mode == MODE,
                CronJob.enabled.is_(True),
            )
        )
        or 0
    )


async def require_schedule_actor(session: AsyncSession, bot: Bot, actor: User) -> UserIdentity:
    """本人仍能用这个机器人：机器人启用、本人启用且在允许名单内、已绑定机器人所在平台的身份。"""
    allowed = (
        await session.scalars(select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id))
    ).all()
    if (
        not bot.enabled
        or bot.platform not in PLATFORMS
        or actor.status != "active"
        or actor.source == "bootstrap"
        or (allowed and actor.id not in allowed)
    ):
        raise ApiError(403, 403, "reminder_actor_unavailable")
    identity = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == actor.id, UserIdentity.platform == bot.platform
        )
    )
    if identity is None:
        raise ApiError(403, 403, "reminder_actor_unbound")
    return identity


# ---- 接收人 -----------------------------------------------------------------------------


async def known_groups(session: AsyncSession, bot: Bot, user_id: uuid.UUID) -> list[str]:
    """本人在里面和这个机器人说过话的群：结果能发进去的只有这些。"""
    identity = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == user_id, UserIdentity.platform == bot.platform
        )
    )
    if identity is None:
        return []
    ids = [identity.platform_user_id] + ([identity.open_id] if identity.open_id else [])
    return list(
        (
            await session.scalars(
                select(InboundEvent.chat_id)
                .where(
                    InboundEvent.bot_id == bot.id,
                    InboundEvent.platform == bot.platform,
                    InboundEvent.chat_type == "group",
                    InboundEvent.kind == "message",
                    or_(
                        InboundEvent.sender_platform_user_id.in_(ids),
                        InboundEvent.sender_open_id.in_(ids),
                    ),
                )
                .distinct()
                .order_by(InboundEvent.chat_id)
                .limit(200)
            )
        ).all()
    )


def _reachable(bot: Bot) -> Any:
    """私聊过这个机器人、仍启用且绑定了平台身份的人：结果能直接送达。"""
    return (
        select(User)
        .join(UserReached, (UserReached.user_id == User.id) & (UserReached.bot_id == bot.id))
        .join(
            UserIdentity,
            (UserIdentity.user_id == User.id) & (UserIdentity.platform == bot.platform),
        )
        .where(User.status == "active", User.source != "bootstrap")
    )


async def reachable_users(
    session: AsyncSession, bot: Bot, query: str = "", limit: int = 20
) -> list[User]:
    stmt = _reachable(bot).order_by(User.display_name, User.login_name).limit(limit)
    text = query.strip()
    if text:
        like = f"%{text}%"
        stmt = stmt.where(
            or_(User.display_name.ilike(like), User.login_name.ilike(like), User.email.ilike(like))
        )
    return list((await session.scalars(stmt)).unique().all())


async def resolve_targets(
    session: AsyncSession,
    bot: Bot,
    owner: uuid.UUID,
    *,
    include_self: bool,
    user_ids: list[str],
    chat_ids: list[str],
) -> tuple[list[uuid.UUID], list[str], list[str]]:
    """校验接收人，返回 (target_users, target_chats, 给人看的接收人说明)。"""
    others: list[uuid.UUID] = []
    for raw in dict.fromkeys(user_ids):
        try:
            uid = uuid.UUID(str(raw))
        except ValueError:
            raise ScheduleError("invalid_recipient", f"接收人 ID 无效：{raw}") from None
        if uid != owner:
            others.append(uid)
    chats = list(dict.fromkeys(chat_ids))
    if len(others) > MAX_RECIPIENTS or len(chats) > MAX_CHATS:
        raise ScheduleError(
            "too_many_recipients", f"最多 {MAX_RECIPIENTS} 位同事、{MAX_CHATS} 个群"
        )
    labels = [SELF_LABEL] if include_self else []
    found = (
        {
            u.id: u
            for u in (await session.scalars(_reachable(bot).where(User.id.in_(others)))).unique()
        }
        if others
        else {}
    )
    for uid in others:
        user = found.get(uid)
        if user is None:
            raise ScheduleError(
                "recipient_unreachable",
                f"接收人 {uid} 不可用：对方需要已启用、绑定平台身份，并私聊过这个机器人",
            )
        labels.append(user.display_name or user.login_name or str(uid))
    if chats:
        allowed = set(await known_groups(session, bot, owner))
        outside = [cid for cid in chats if cid not in allowed]
        if outside:
            raise ScheduleError(
                "chat_not_allowed",
                "只能发到你在里面和这个机器人说过话的群：" + "、".join(outside),
            )
        labels.extend(f"群 {cid}" for cid in chats)
    if not include_self and not others and not chats:
        raise ScheduleError("no_recipient", "至少需要一个接收人")
    return ([owner] if include_self else []) + others, chats, labels


# ---- 草稿与确认卡片 ----------------------------------------------------------------------


def _summary(draft: dict[str, Any], next_runs: list[datetime]) -> list[str]:
    run_at = datetime.fromisoformat(draft["run_at"]) if draft.get("run_at") else None
    lines = [
        "执行时间：" + describe(draft["schedule_kind"], draft["cron_expression"], run_at),
        "接下来：" + "、".join(shown(at) for at in next_runs),
        "接收人：" + "、".join(draft.get("recipient_labels") or [SELF_LABEL]),
    ]
    if draft.get("expires_at"):
        lines.append("到期：" + shown(datetime.fromisoformat(draft["expires_at"])))
    if draft.get("precheck_script"):
        lines.append("执行前检查：每次执行前先运行，判断不需要执行时跳过")
    return lines


def _notice(draft: dict[str, Any]) -> str:
    only_self = draft.get("target_users", [draft["user_id"]]) == [draft["user_id"]] and not (
        draft.get("target_chats")
    )
    target = (
        "结果只发到这个私聊。"
        if only_self
        else "结果会发给上面列出的接收人；发给别人时，执行中不会使用你的飞书或企业微信个人工具。"
    )
    return (
        f"任务归你本人所有，以你的身份运行。{target}"
        f"每人最多 {MAX_ACTIVE} 个启用中的任务，可在“我的定时任务”暂停或删除。确认有效期 30 分钟。"
    )


def proposal_card(
    origin_task_id: int, state_id: uuid.UUID, draft: dict[str, Any], next_runs: list[datetime]
) -> dict[str, Any]:
    title = ACTION_TITLES.get(draft.get("action") or "create", "创建")

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

    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**{draft['name']}**\n" + "\n".join(_summary(draft, next_runs)),
        },
        {"tag": "markdown", "content": "**每次执行的指令**\n" + draft["prompt"]},
    ]
    if draft.get("precheck_script"):
        elements.append(
            {
                "tag": "markdown",
                "content": "**执行前检查**\n```python\n" + draft["precheck_script"] + "\n```",
            }
        )
    elements += [
        button(f"确认{title}", "confirm", "primary"),
        button("取消", "cancel", "default"),
        {"tag": "markdown", "content": _notice(draft)},
    ]
    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"{title}定时任务"},
        },
        "body": {"elements": elements},
    }


def wecom_card_task_id(origin_task_id: int, state_id: uuid.UUID) -> str:
    return f"{WECOM_CARD_PREFIX}@{origin_task_id}@{state_id.hex}"


def parse_wecom_card_task_id(task_id: str) -> tuple[int, uuid.UUID] | None:
    parts = task_id.split("@")
    if len(parts) != 3 or parts[0] != WECOM_CARD_PREFIX or not parts[1].isdigit():
        return None
    try:
        return int(parts[1]), uuid.UUID(hex=parts[2])
    except ValueError:
        return None


def wecom_details(draft: dict[str, Any], next_runs: list[datetime]) -> str:
    """企业微信卡片字数很紧，完整内容放在卡片前这条消息里。"""
    title = ACTION_TITLES.get(draft.get("action") or "create", "创建")
    text = (
        f"**{title}定时任务：{draft['name']}**\n"
        + "\n".join(f"> {line}" for line in _summary(draft, next_runs))
        + "\n\n**每次执行的指令**\n"
        + str(draft["prompt"])
    )
    if draft.get("precheck_script"):
        text += "\n\n**执行前检查**\n```\n" + draft["precheck_script"] + "\n```"
    return text + "\n\n" + _notice(draft) + "\n请在下面的卡片上确认。"


def wecom_proposal_card(task_id: str, draft: dict[str, Any], icon_url: str = "") -> dict[str, Any]:
    from coreman.core.wecom.cards import clip

    title = ACTION_TITLES.get(draft.get("action") or "create", "创建")
    source: dict[str, Any] = {"desc": "定时任务"}
    if icon_url:
        source["icon_url"] = icon_url
    return {
        "card_type": "button_interaction",
        "source": source,
        "main_title": {"title": clip(f"{title}定时任务", 26), "desc": clip(draft["name"], 30)},
        "sub_title_text": "详情见上一条消息。任务以你的身份运行，确认有效期 30 分钟。",
        "button_list": [
            {"text": f"确认{title}", "style": 1, "key": "confirm"},
            {"text": "取消", "style": 2, "key": "cancel"},
        ],
        "task_id": task_id,
    }


async def _pending(
    session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID, now: datetime
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(InteractionState)
            .where(
                InteractionState.kind == KIND,
                InteractionState.bot_id == bot_id,
                InteractionState.state["user_id"].astext == str(user_id),
                InteractionState.status == "open",
                InteractionState.expires_at > now,
            )
        )
        or 0
    )


async def own_job(
    session: AsyncSession,
    bot: Bot,
    user_id: uuid.UUID,
    job_id: uuid.UUID | str,
    *,
    lock: bool = False,
) -> CronJob | None:
    try:
        key = uuid.UUID(str(job_id))
    except ValueError:
        return None
    stmt = select(CronJob).where(
        CronJob.id == key,
        CronJob.bot_id == bot.id,
        CronJob.created_by == user_id,
        CronJob.execution_mode == MODE,
    )
    job: CronJob | None = await session.scalar(stmt.with_for_update() if lock else stmt)
    return job


async def open_draft(
    session: AsyncSession,
    origin: Origin,
    args: Draft,
    *,
    action: str = "create",
    job: CronJob | None = None,
    icon_url: str = "",
) -> dict[str, Any]:
    """校验草稿、存起来并发确认卡片。返回给模型的只有结果摘要，不会替用户确认。"""
    now = utcnow()
    bot = origin.bot
    try:
        result = plan(args.cron_expression, args.run_at, now)
        expires = expiry(args.expires_at, result, now)
        script = check_precheck(args.precheck_script)
        users, chats, labels = await resolve_targets(
            session,
            bot,
            origin.user_id,
            include_self=args.include_self,
            user_ids=args.recipient_user_ids,
            chat_ids=list(args.recipient_chat_ids),
        )
    except ScheduleError as exc:
        return {"error": exc.code, "message": exc.message}
    if (job is None or not job.enabled) and await active_count(
        session, bot.id, origin.user_id
    ) >= MAX_ACTIVE:
        return {
            "error": "too_many_schedules",
            "message": f"每人最多保留 {MAX_ACTIVE} 个启用中的定时任务，请先暂停或删除一些",
        }
    if await _pending(session, bot.id, origin.user_id, now) >= MAX_PENDING:
        return {"error": "too_many_pending", "message": "待确认的定时任务太多，请先确认或取消"}
    draft = {
        "user_id": str(origin.user_id),
        "chat_id": origin.chat_id,
        "origin_task_id": origin.task.id,
        "platform": bot.platform,
        "action": action,
        "job_id": str(job.id) if job else None,
        "job_version": job.version if job else None,
        "name": args.name,
        "prompt": args.prompt,
        "schedule_kind": result.schedule_kind,
        "cron_expression": result.cron_expression,
        "run_at": result.run_at.isoformat() if result.run_at else None,
        "expires_at": expires.isoformat() if expires else None,
        "target_users": [str(uid) for uid in users],
        "target_chats": chats,
        "recipient_labels": labels,
        "precheck_script": script,
    }
    state = await interactions.open_state(
        session,
        bot_id=bot.id,
        kind=KIND,
        scope_key=f"{bot.id}:{origin.user_id}:{uuid.uuid4()}",
        state=draft,
        expires_at=now + PROPOSAL_TTL,
    )
    if bot.platform == "wecom":
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="wecom",
            kind="send",
            dedupe_key=f"{origin.task.id}:schedule:{state.id}:details",
            target={"chat_id": origin.chat_id},
            payload={"markdown": wecom_details(draft, result.next_runs)},
        )
        card = wecom_proposal_card(wecom_card_task_id(origin.task.id, state.id), draft, icon_url)
    else:
        card = {
            **proposal_card(origin.task.id, state.id, draft, result.next_runs),
            "task_id": f"personal:{origin.task.id}",
        }
    await outbox.add(
        session,
        bot_id=bot.id,
        platform=bot.platform,
        kind="send",
        dedupe_key=f"{origin.task.id}:schedule:{state.id}",
        target={"chat_id": origin.chat_id},
        payload={"card": card},
    )
    return {
        "status": "awaiting_confirmation",
        "draft_id": str(state.id),
        "schedule": describe(result.schedule_kind, result.cron_expression, result.run_at),
        "next_runs": [shown(at) for at in result.next_runs],
        "recipients": labels,
        "message": "已发送确认卡片；用户点确认后才会生效，在此之前不要说已经完成。",
    }


def draft_of(job: CronJob) -> dict[str, Any]:
    """已有任务的草稿字段：重新启用时原样确认，修改时在其上覆盖。"""
    owner = job.created_by
    return {
        "name": job.name,
        "prompt": job.prompt,
        "cron_expression": job.cron_expression if job.schedule_kind == "recurring" else None,
        "run_at": job.run_at.isoformat() if job.schedule_kind == "once" and job.run_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "include_self": owner in job.target_users,
        "recipient_user_ids": [str(uid) for uid in job.target_users if uid != owner],
        "recipient_chat_ids": list(job.target_chats or []),
        "precheck_script": job.precheck_script,
    }


async def propose(
    session: AsyncSession, scope: Origin, arguments: dict[str, Any]
) -> dict[str, Any]:
    """MCP 工具入口：只收名称、指令与时间，结果只发本人私聊。"""
    try:
        args = Propose.model_validate(arguments)
    except ValidationError:
        return {
            "error": "invalid_tool_or_arguments",
            "message": "需要 name、prompt，以及 cron_expression 或 run_at 其中之一",
        }
    result = await open_draft(session, scope, Draft(**args.model_dump()))
    result.pop("draft_id", None)
    result.pop("recipients", None)
    return result


def job_view(job: CronJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "name": job.name,
        "enabled": job.enabled,
        "schedule": describe(job.schedule_kind, job.cron_expression, job.run_at),
        "next_run": shown(job.next_run_at) if job.enabled else None,
        "last_run_at": shown(job.last_run_at) if job.last_run_at else None,
        "last_status": job.last_status,
        "running": job.running_task_id is not None,
        "run_requested": job.force_run_at is not None,
        **draft_of(job),
    }


async def list_own(session: AsyncSession, scope: Origin) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(CronJob)
            .where(
                CronJob.bot_id == scope.bot.id,
                CronJob.created_by == scope.user_id,
                (CronJob.execution_mode == MODE)
                | ((CronJob.execution_mode == REMINDER_MODE) & CronJob.enabled.is_(True)),
            )
            .order_by(CronJob.enabled.desc(), CronJob.created_at.desc())
            .limit(50)
        )
    ).all()
    return {
        "items": [
            {
                "type": "定时任务" if row.execution_mode == MODE else "一次性提醒",
                "name": row.name if row.execution_mode == MODE else row.prompt,
                "schedule": describe(row.schedule_kind, row.cron_expression, row.run_at),
                "enabled": row.enabled,
                "next_run": shown(row.next_run_at) if row.enabled else None,
                "last_status": row.last_status,
            }
            for row in rows
        ]
    }


async def dispatch(
    session: AsyncSession, scope: Origin, name: str, arguments: Any
) -> dict[str, Any]:
    if scope.scheduled or not isinstance(arguments, dict):
        return {"error": "invalid_tool_or_arguments"}
    if name == "schedule_propose":
        return await propose(session, scope, arguments)
    if name == "schedule_list" and not arguments:
        return await list_own(session, scope)
    return {"error": "invalid_tool_or_arguments"}


async def apply_confirmed(
    session: AsyncSession,
    bot: Bot,
    actor: User,
    chat_id: str,
    draft: dict[str, Any],
    now: datetime,
) -> CronJob:
    """本人点了确认：按当时的规则重新校验再落库。调用方已证明点击者与私聊来源。"""
    await require_schedule_actor(session, bot, actor)
    result = plan(draft.get("cron_expression") or None, draft.get("run_at"), now)
    expires = expiry(draft.get("expires_at"), result, now)
    script = check_precheck(draft.get("precheck_script"))
    owner = str(actor.id)
    # 升级前存下的草稿没有接收人字段，按只发本人处理。
    wanted = [str(uid) for uid in draft.get("target_users") or [owner]]
    users, chats, _ = await resolve_targets(
        session,
        bot,
        actor.id,
        include_self=owner in wanted,
        user_ids=[uid for uid in wanted if uid != owner],
        chat_ids=list(draft.get("target_chats") or []),
    )
    job: CronJob | None = None
    if (draft.get("action") or "create") != "create":
        job = await own_job(session, bot, actor.id, draft.get("job_id") or "", lock=True)
        if job is None:
            raise ScheduleError("not_found", "这个定时任务已不存在")
        if job.version != draft.get("job_version"):
            raise ScheduleError("changed", "任务在确认前已被改动，请重新发起")
        if job.force_run_at is not None or job.running_task_id is not None:
            raise ScheduleError("busy", "任务正在执行或等待立即运行，请结束后再改")
    if (job is None or not job.enabled) and await active_count(
        session, bot.id, actor.id
    ) >= MAX_ACTIVE:
        raise ScheduleError(
            "too_many_schedules", f"每人最多保留 {MAX_ACTIVE} 个启用中的定时任务，请先暂停或删除"
        )
    # 与本人提醒一样：只有经过验证的本人私聊能确立这个投递地址。
    await session.execute(
        insert(UserReached)
        .values(bot_id=bot.id, user_id=actor.id, platform_chat_id=chat_id)
        .on_conflict_do_update(
            index_elements=["bot_id", "user_id"], set_={"platform_chat_id": chat_id}
        )
    )
    values: dict[str, Any] = {
        "name": str(draft["name"])[:NAME_MAX],
        "reminder_chat_id": chat_id,
        "schedule_kind": result.schedule_kind,
        "cron_expression": result.cron_expression,
        "run_at": result.run_at,
        "next_run_at": result.next_runs[0],
        "consumed_at": None,
        "timezone": TIMEZONE,
        "prompt": str(draft["prompt"])[:PROMPT_MAX],
        "target_users": users,
        "target_chats": chats,
        "precheck_script": script,
        "expires_at": expires,
        "enabled": True,
    }
    if job is None:
        job = CronJob(bot_id=bot.id, created_by=actor.id, execution_mode=MODE, **values)
        session.add(job)
    else:
        for key, value in values.items():
            setattr(job, key, value)
        job.updated_at = now
    await session.flush()
    return job


create_confirmed = apply_confirmed


async def require_personal(session: AsyncSession, job: CronJob, bot: Bot, actor: User) -> Speaker:
    """调度与执行前的边界：仍归本人、接收人仍在允许范围内、本人仍可使用这个机器人。"""
    identity = await require_schedule_actor(session, bot, actor)
    reached = await session.get(UserReached, (bot.id, actor.id), populate_existing=True)
    if (
        job.execution_mode != MODE
        or job.bot_id != bot.id
        or job.created_by != actor.id
        or not (job.target_users or job.target_chats)
        or len(set(job.target_users) - {actor.id}) > MAX_RECIPIENTS
        or len(job.target_chats or []) > MAX_CHATS
        or job.notify_emails
        or job.notify_webhook
        or job.notify_webhook_url_enc
        or job.system_prompt
        or job.force_run_by not in (None, actor.id)
        or not job.reminder_chat_id
        or not 1 <= len(job.prompt) <= PROMPT_MAX
        or reached is None
        or reached.platform_chat_id != job.reminder_chat_id
    ):
        raise ApiError(403, 403, "invalid_personal_schedule")
    if job.target_chats and not set(job.target_chats) <= set(
        await known_groups(session, bot, actor.id)
    ):
        raise ApiError(403, 403, "personal_schedule_chat_unavailable")
    return Speaker(identity.platform_user_id, actor.id, actor.login_name, actor.display_name)


# ---- 技能凭据 -----------------------------------------------------------------------------
# 私聊里下发给 CLI 的短期凭据：只指明任务、本人与签发时的会话。每次调用都按任务重新核验持久化
# 的私聊来源（发言者就是本人），会话切换、任务结束、权限收回都会让它失效。定时执行不下发。

AAD = "personal_schedules.capability.v1"
ENV_PREFIX = "COREMAN_SCHEDULE_"
CAPABILITY_TTL = 1800


@dataclass(frozen=True)
class Capability:
    task_id: int
    actor: str
    session_id: uuid.UUID


def issue_capability(
    cipher: Cipher, *, task_id: int, user_id: str, base_session_id: uuid.UUID
) -> str:
    claims = {
        "task": task_id,
        "actor": user_id,
        "session": str(base_session_id),
        "exp": time.time() + CAPABILITY_TTL,
    }
    return cipher.encrypt(json.dumps(claims), AAD)


def read_capability(cipher: Cipher, token: str) -> Capability:
    data = json.loads(cipher.decrypt(token, AAD))
    if not isinstance(data, dict) or data["exp"] <= time.time():
        raise ValueError("expired_capability")
    return Capability(int(data["task"]), str(uuid.UUID(data["actor"])), uuid.UUID(data["session"]))


async def origin_scope(session: AsyncSession, task_id: int, actor: str) -> Origin:
    """本人仍在进行中的已验证私聊。平台由任务所属机器人决定，两边各自核验来源。"""
    task = await session.get(Task, task_id, populate_existing=True)
    bot = await session.get(Bot, task.bot_id) if task else None
    if bot is None or bot.platform not in PLATFORMS:
        raise ValueError("private_human_task_required")
    if bot.platform == "wecom":
        from coreman.core.wecom_personal import policy as wecom_policy

        return await wecom_policy.task_scope(session, task_id, actor)
    from coreman.core.feishu_personal import policy as feishu_policy

    return await feishu_policy.task_scope(session, task_id, actor)
