"""本人专属的 AI 定时任务：模型只拟草稿，本人在私聊里点卡片确认后才创建。

任务归本人所有、以本人身份运行、结果只发回本人与机器人的私聊，所以不需要机器人管理员
权限；代价（每次执行都调模型）用数量上限和最短间隔约束。已授权飞书的人，任务运行时同样
能以本人身份读飞书（见 feishu_personal.policy.scheduled_scope）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.chat import interactions
from coreman.core.cron.schedule import next_run
from coreman.core.db.models import Bot, CronJob, InteractionState, User, UserIdentity, UserReached
from coreman.core.errors import ApiError
from coreman.core.feishu_personal.policy import Scope
from coreman.core.prompting import Speaker
from coreman.core.reminders import MODE as REMINDER_MODE
from coreman.core.reminders import require_actor
from coreman.core.timeutils import utcnow

MODE = "personal_ai"
KIND = "personal_schedule"
TIMEZONE = "Asia/Shanghai"
MAX_ACTIVE = 10
MAX_PENDING = 5
MIN_INTERVAL = timedelta(hours=1)
ONCE_MIN_LEAD = timedelta(minutes=1)
ONCE_MAX_LEAD = timedelta(days=30)
PROPOSAL_TTL = timedelta(minutes=30)
NAME_MAX, PROMPT_MAX = 40, 2000
# 检查最短间隔时往后看多少次触发；一年以外的触发不再看。
INTERVAL_SAMPLES = 60
CARD_EVENT = "personal_schedule"


class ScheduleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]
Instruction = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PROMPT_MAX)
]
Cron = Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)]


class Propose(_Arguments):
    name: Name = Field(description="Short task name shown to the user")
    prompt: Instruction = Field(
        description="Complete, self-contained instruction executed on every run"
    )
    cron_expression: Cron | None = Field(
        default=None,
        description="Recurring: five-field cron (minute hour day month weekday) in Beijing time",
    )
    run_at: Annotated[str, StringConstraints(max_length=40)] | None = Field(
        default=None,
        description="One-off: ISO 8601 time with timezone offset, e.g. 2026-01-02T09:00:00+08:00",
    )

    @model_validator(mode="after")
    def one_schedule(self) -> Propose:
        if (self.cron_expression is None) == (self.run_at is None):
            raise ValueError("exactly one of cron_expression and run_at is required")
        return self


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
        try:
            parsed = datetime.fromisoformat(run_at)
        except ValueError as exc:
            raise ScheduleError("invalid_schedule", "run_at 需要带时区的 ISO 8601 时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ScheduleError("invalid_schedule", "run_at 需要带时区的 ISO 8601 时间")
        run_at = parsed
    if run_at is None:
        raise ScheduleError("invalid_schedule", "需要 cron_expression 或 run_at")
    at = run_at.astimezone(UTC)
    if not now + ONCE_MIN_LEAD <= at <= now + ONCE_MAX_LEAD:
        raise ScheduleError("run_at_out_of_range", "一次性任务的时间需要在 1 分钟之后、30 天之内")
    return Plan("once", "", at, [at])


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


def proposal_card(
    origin_task_id: int, state_id: uuid.UUID, draft: dict[str, Any], next_runs: list[datetime]
) -> dict[str, Any]:
    run_at = datetime.fromisoformat(draft["run_at"]) if draft.get("run_at") else None
    when = describe(draft["schedule_kind"], draft["cron_expression"], run_at)
    upcoming = "、".join(shown(at) for at in next_runs)

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

    return {
        "schema": "2.0",
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "创建定时任务"}},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**{draft['name']}**\n执行时间：{when}\n接下来：{upcoming}",
                },
                {"tag": "markdown", "content": "**每次执行的指令**\n" + draft["prompt"]},
                button("确认创建", "confirm", "primary"),
                button("取消", "cancel", "default"),
                {
                    "tag": "markdown",
                    "content": (
                        "任务归你本人所有，以你的身份运行，结果只发到这个私聊。"
                        f"每人最多 {MAX_ACTIVE} 个启用中的任务，可在“我的定时任务”暂停或删除。"
                        "确认有效期 30 分钟。"
                    ),
                },
            ]
        },
    }


async def propose(session: AsyncSession, scope: Scope, arguments: dict[str, Any]) -> dict[str, Any]:
    """存草稿并发确认卡片。返回给模型的只有结果摘要，不会替用户确认。"""
    try:
        args = Propose.model_validate(arguments)
    except ValidationError:
        return {
            "error": "invalid_tool_or_arguments",
            "message": "需要 name、prompt，以及 cron_expression 或 run_at 其中之一",
        }
    now = utcnow()
    try:
        result = plan(args.cron_expression, args.run_at, now)
    except ScheduleError as exc:
        return {"error": exc.code, "message": exc.message}
    if await active_count(session, scope.bot.id, scope.user_id) >= MAX_ACTIVE:
        return {
            "error": "too_many_schedules",
            "message": f"每人最多保留 {MAX_ACTIVE} 个启用中的定时任务，请先暂停或删除一些",
        }
    pending = await session.scalar(
        select(func.count())
        .select_from(InteractionState)
        .where(
            InteractionState.kind == KIND,
            InteractionState.bot_id == scope.bot.id,
            InteractionState.state["user_id"].astext == str(scope.user_id),
            InteractionState.status == "open",
            InteractionState.expires_at > now,
        )
    )
    if pending and pending >= MAX_PENDING:
        return {"error": "too_many_pending", "message": "待确认的定时任务太多，请先确认或取消"}
    draft = {
        "user_id": str(scope.user_id),
        "chat_id": scope.chat_id,
        "origin_task_id": scope.task.id,
        "name": args.name,
        "prompt": args.prompt,
        "schedule_kind": result.schedule_kind,
        "cron_expression": result.cron_expression,
        "run_at": result.run_at.isoformat() if result.run_at else None,
    }
    state = await interactions.open_state(
        session,
        bot_id=scope.bot.id,
        kind=KIND,
        scope_key=f"{scope.bot.id}:{scope.user_id}:{uuid.uuid4()}",
        state=draft,
        expires_at=now + PROPOSAL_TTL,
    )
    await outbox.add(
        session,
        bot_id=scope.bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"{scope.task.id}:schedule:{state.id}",
        target={"chat_id": scope.chat_id},
        payload={
            "card": {
                **proposal_card(scope.task.id, state.id, draft, result.next_runs),
                "task_id": f"personal:{scope.task.id}",
            }
        },
    )
    return {
        "status": "awaiting_confirmation",
        "schedule": describe(result.schedule_kind, result.cron_expression, result.run_at),
        "next_runs": [shown(at) for at in result.next_runs],
        "message": "已发送确认卡片；用户点“确认创建”后才会生效。",
    }


async def list_own(session: AsyncSession, scope: Scope) -> dict[str, Any]:
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
    session: AsyncSession, scope: Scope, name: str, arguments: Any
) -> dict[str, Any]:
    if scope.scheduled or not isinstance(arguments, dict):
        return {"error": "invalid_tool_or_arguments"}
    if name == "schedule_propose":
        return await propose(session, scope, arguments)
    if name == "schedule_list" and not arguments:
        return await list_own(session, scope)
    return {"error": "invalid_tool_or_arguments"}


async def create_confirmed(
    session: AsyncSession,
    bot: Bot,
    actor: User,
    chat_id: str,
    draft: dict[str, Any],
    now: datetime,
) -> CronJob:
    """本人点了确认：按当时的规则重新校验再落库。调用方已证明点击者与私聊来源。"""
    await require_actor(session, bot, actor)
    result = plan(draft.get("cron_expression") or None, draft.get("run_at"), now)
    if await active_count(session, bot.id, actor.id) >= MAX_ACTIVE:
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
    job = CronJob(
        bot_id=bot.id,
        created_by=actor.id,
        name=str(draft["name"])[:NAME_MAX],
        execution_mode=MODE,
        reminder_chat_id=chat_id,
        schedule_kind=result.schedule_kind,
        cron_expression=result.cron_expression,
        run_at=result.run_at,
        next_run_at=result.next_runs[0],
        timezone=TIMEZONE,
        prompt=str(draft["prompt"])[:PROMPT_MAX],
        target_users=[actor.id],
    )
    session.add(job)
    await session.flush()
    return job


async def require_personal(session: AsyncSession, job: CronJob, bot: Bot, actor: User) -> Speaker:
    """调度与执行前的边界：仍归本人、仍只发本人私聊、本人仍可使用这个机器人。"""
    await require_actor(session, bot, actor)
    reached = await session.get(UserReached, (bot.id, actor.id), populate_existing=True)
    if (
        job.execution_mode != MODE
        or job.bot_id != bot.id
        or job.created_by != actor.id
        or job.target_users != [actor.id]
        or job.target_chats
        or job.notify_emails
        or job.notify_webhook
        or job.notify_webhook_url_enc
        or job.system_prompt
        or job.precheck_script
        or job.force_run_by not in (None, actor.id)
        or not job.reminder_chat_id
        or not 1 <= len(job.prompt) <= PROMPT_MAX
        or reached is None
        or reached.platform_chat_id != job.reminder_chat_id
    ):
        raise ApiError(403, 403, "invalid_personal_schedule")
    identity = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == actor.id, UserIdentity.platform == bot.platform
        )
    )
    if identity is None:
        raise ApiError(403, 403, "reminder_actor_unbound")
    return Speaker(identity.platform_user_id, actor.id, actor.login_name, actor.display_name)
