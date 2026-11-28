"""知识库的来源：读什么、排除什么都在这里由代码决定，不靠提示词。

第一期四类来源：各 AI 员工的规则文档、业务系统目录、名录、任务平台。群对话是第二期。

永不读：个人助理（员工、任务都排除）、私聊和单人群里发起的任务、镜像进任务平台的账本记录
（业务系统任务、求助）。成员的邮箱、手机号不进名录。

进度按「单元」记：一个单元要么全部抽完、要么不动进度。文档类来源按 Markdown 标题切段、
逐段记哈希，下次只抽新出现的段；任务按完成时间往后走。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bots import rules_backup
from coreman.core.db.models import (
    Bot,
    BotMember,
    BotSystemGrant,
    BusinessSystem,
    InboundEvent,
    RelayServer,
    SystemCatalog,
    Team,
    User,
    WorkTask,
)
from coreman.core.logging import get_logger
from coreman.core.relay.agent_client import AgentError

log = get_logger(__name__)

SOURCES = ("rules", "catalog", "roster", "task")
# 一批交给抽取回合的材料上限（字符）。
BATCH_CHARS = 12_000
# 每个员工最多读这么多规则文档；自带代码仓库的员工 docs/ 可能很大。
RULES_FILES_PER_BOT = 40
RULES_CHARS_PER_BOT = 300_000
PROMPT_CHARS = 4_000
MODULE_OPERATIONS = 60
# 一次最多往后看这么多条已完成的任务。
TASK_SCAN = 2_000
TASK_FIELD_CHARS = 2_000
TASK_REPORT_CHARS = 3_000
# 任务来源：网页上建的、群里调度出来的，以及它们拆出的子任务。api / collaboration 是别处账本的
# 镜像（业务系统任务、求助），内容不属于任务平台，不读。
TASK_SOURCES = ("web", "dispatch", "agent")
RISKS = {"read": "读", "write": "写", "destructive": "不可恢复", "financial": "资金"}


@dataclass(frozen=True)
class Batch:
    """交给一轮抽取的材料。`refs` 是这批材料里可以引用的来源，条目必须落在其中一个上。"""

    source: str
    refs: tuple[str, ...]
    label: str
    text: str
    dates: Mapping[str, date] = field(default_factory=dict)


@dataclass(frozen=True)
class Unit:
    """进度的最小单位：`batches` 全部处理完，才把 `mark` 写到进度的 `key` 上。"""

    source: str
    key: str
    mark: Any
    batches: tuple[Batch, ...] = ()


@dataclass
class Collected:
    units: list[Unit] = field(default_factory=list)
    # 读不到的员工、不够条件的任务等，给运行记录看。
    notes: dict[str, int] = field(default_factory=dict)

    def note(self, name: str, count: int = 1) -> None:
        if count:
            self.notes[name] = self.notes.get(name, 0) + count


def sections(text: str) -> list[str]:
    """按 Markdown 标题切段，标题行归到它下面那一段。"""
    parts: list[list[str]] = [[]]
    for line in text.splitlines():
        if line.startswith("#") and any(item.strip() for item in parts[-1]):
            parts.append([])
        parts[-1].append(line)
    return [joined for part in parts if (joined := "\n".join(part).strip())]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def pack(parts: list[str], limit: int = BATCH_CHARS) -> list[str]:
    """把若干段拼成不超过 `limit` 的几批；单段超长的按长度硬切。"""
    out: list[str] = []
    current = ""
    for part in parts:
        while len(part) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(part[:limit])
            part = part[limit:]
        if current and len(current) + len(part) + 2 > limit:
            out.append(current)
            current = ""
        current = f"{current}\n\n{part}" if current else part
    if current:
        out.append(current)
    return out


def doc_unit(
    source: str, key: str, ref: str, label: str, text: str, seen: object, today: date
) -> Unit | None:
    """一份文档这次要抽的部分：只有上次没见过的段。没有变化返回 None。"""
    parts = sections(text)
    hashes = [digest(part) for part in parts]
    known = set(seen) if isinstance(seen, list) else set()
    if set(hashes) == known:
        return None
    fresh = [part for part, value in zip(parts, hashes, strict=True) if value not in known]
    batches = tuple(
        Batch(source=source, refs=(ref,), label=label, text=chunk, dates={ref: today})
        for chunk in pack(fresh)
    )
    return Unit(source=source, key=key, mark=hashes, batches=batches)


def clip(value: object, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def rules(
    factory: async_sessionmaker[AsyncSession], target: Bot, cursor: Mapping[str, Any], today: date
) -> Collected:
    """各 AI 员工的 AGENTS.md 与 docs/（不含承接员工自己：它的 docs/ 就是 Wiki）。"""
    async with factory() as session:
        rows = (
            await session.execute(
                select(Bot, RelayServer)
                .outerjoin(RelayServer, RelayServer.id == Bot.relay_server_id)
                # 个人助理的规则只属于本人，一概不读。
                .where(Bot.kind == "standard", Bot.enabled, Bot.id != target.id)
                .order_by(Bot.bot_key)
            )
        ).all()
        session.expunge_all()
    out = Collected()
    for bot, relay in rows:
        if not (
            bot.working_dir
            and bot.workspace_state == "ready"
            and relay is not None
            and relay.is_active
            and relay.runtime_node_id is not None
        ):
            out.note("rules_inactive")
            continue
        try:
            async with asyncio.timeout(rules_backup.BOT_TIMEOUT_SECONDS):
                found = await rules_backup.collect(relay, bot, own_repo=True)
        except (AgentError, rules_backup.BackupError, TimeoutError) as exc:
            out.note("rules_unreadable")
            log.warning("kb_rules_unreadable", bot_key=bot.bot_key, reason=str(exc) or "timeout")
            continue
        files = dict(found.files) if found is not None else {}
        if "AGENTS.md" in files:
            # 两份都是文件时内容通常相同，只读正本。
            files.pop("CLAUDE.md", None)
        ordered = sorted(files, key=lambda path: (path not in rules_backup.RULE_FILES, path))
        total = 0
        for path in ordered[:RULES_FILES_PER_BOT]:
            total += len(files[path])
            if total > RULES_CHARS_PER_BOT:
                out.note("rules_truncated")
                break
            key = f"{bot.bot_key}/{path}"
            unit = doc_unit(
                "rules",
                key,
                f"rules:{key}",
                f"AI 员工「{bot.name}」的规则文档 {path}",
                files[path],
                cursor.get(key),
                today,
            )
            if unit is not None:
                out.units.append(unit)
        if len(ordered) > RULES_FILES_PER_BOT:
            out.note("rules_truncated")
    return out


def render_system(
    system: BusinessSystem, compiled: Mapping[str, Any] | None, bots: list[str]
) -> str:
    """一个业务系统的说明：不带地址，只有它做什么、谁能用、有哪些模块和操作。"""
    env = "，测试环境" if system.environment == "test" else ""
    lines = [f"# 业务系统「{system.name}」（{system.key}{env}）"]
    if system.description:
        lines.append(f"说明：{clip(system.description, 1000)}")
    if system.default_for_all_bots:
        lines.append("可用的 AI 员工：所有 AI 员工")
    elif bots:
        lines.append(f"可用的 AI 员工：{'、'.join(bots)}")
    compiled = compiled or {}
    if guide := clip(compiled.get("guide"), 2000):
        lines.append(f"使用指引：{guide}")
    operations, modules = compiled.get("operations"), compiled.get("modules")
    operations = operations if isinstance(operations, dict) else {}
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, dict):
            continue
        name = clip(module.get("display_name"), 60) or clip(module.get("name"), 60)
        lines.append(f"\n## 模块「{name}」")
        if description := clip(module.get("description"), 200):
            lines.append(description)
        if limits := clip(module.get("limits"), 200):
            lines.append(f"限制：{limits}")
        ids = module.get("operations")
        for op_id in (ids if isinstance(ids, list) else [])[:MODULE_OPERATIONS]:
            op = operations.get(op_id)
            if isinstance(op, dict) and not op.get("deprecated"):
                summary = clip(op.get("summary"), 120) or str(op_id)
                lines.append(f"- {summary}（{RISKS.get(str(op.get('risk')), '写')}）")
    return "\n".join(lines)


async def catalog(session: AsyncSession, cursor: Mapping[str, Any], today: date) -> Collected:
    systems = (
        (
            await session.execute(
                select(BusinessSystem)
                .where(BusinessSystem.enabled)
                .order_by(BusinessSystem.sort_order, BusinessSystem.key)
            )
        )
        .scalars()
        .all()
    )
    compiled = {
        row.system_key: row.compiled
        for row in (await session.execute(select(SystemCatalog))).scalars()
    }
    granted: dict[str, list[str]] = {}
    for system_key, name in (
        await session.execute(
            select(BotSystemGrant.system_key, Bot.name)
            .join(Bot, Bot.id == BotSystemGrant.bot_id)
            .where(Bot.kind == "standard", Bot.enabled)
            .order_by(Bot.name)
        )
    ).all():
        granted.setdefault(system_key, []).append(name)
    out = Collected()
    for system in systems:
        text = render_system(system, compiled.get(system.key), granted.get(system.key, []))
        unit = doc_unit(
            "catalog",
            system.key,
            f"catalog:{system.key}",
            f"业务系统「{system.name}」的目录",
            text,
            cursor.get(system.key),
            today,
        )
        if unit is not None:
            out.units.append(unit)
    return out


async def roster(
    session: AsyncSession, target: Bot, cursor: Mapping[str, Any], today: date
) -> Collected:
    """团队、成员（只有姓名、职位、团队）和 AI 员工（说明、职责、谁创建、谁管理）。"""
    teams = {
        team.id: team.name_zh
        for team in (
            await session.execute(select(Team).where(Team.enabled).order_by(Team.sort_order))
        ).scalars()
    }
    people = (
        await session.execute(
            select(User.id, User.display_name, User.position, User.team_id)
            # 服务账号不是人；邮箱、手机号、技能自述都不取。
            .where(User.status == "active", User.source != "service")
            .order_by(User.display_name)
        )
    ).all()
    names = {row.id: row.display_name for row in people}
    lines = ["# 团队与成员"]
    by_team: dict[uuid.UUID | None, list[str]] = {}
    for row in people:
        team = row.team_id if row.team_id in teams else None
        position = f"（{clip(row.position, 60)}）" if row.position else ""
        by_team.setdefault(team, []).append(f"- {row.display_name}{position}")
    for team_id, title in [*teams.items(), (None, "")]:
        members = by_team.get(team_id)
        if members:
            lines.append(f"\n## 团队「{title}」" if team_id else "\n## 没有分到团队的成员")
            lines.extend(members)
    bots = (
        (
            await session.execute(
                select(Bot)
                .where(Bot.kind == "standard", Bot.enabled, Bot.id != target.id)
                .order_by(Bot.name)
            )
        )
        .scalars()
        .all()
    )
    admins: dict[uuid.UUID, list[str]] = {}
    for bot_id, user_id in (
        await session.execute(select(BotMember.bot_id, BotMember.user_id))
    ).all():
        if user_id in names:
            admins.setdefault(bot_id, []).append(names[user_id])
    for bot in bots:
        lines.append(f"\n## AI 员工「{bot.name}」")
        lines.append(f"- 标识：{bot.bot_key}")
        if bot.group_role == "dispatcher":
            lines.append("- 群聊角色：调度员")
        if bot.team_id in teams:
            lines.append(f"- 团队：{teams[bot.team_id]}")
        if bot.created_by in names:
            lines.append(f"- 创建人：{names[bot.created_by]}")
        if admins.get(bot.id):
            lines.append(f"- 管理成员：{'、'.join(sorted(admins[bot.id]))}")
        if bot.description:
            lines.append(f"- 说明：{clip(bot.description, 500)}")
        if bot.group_observe_scope:
            lines.append(f"- 群聊职责：{clip(bot.group_observe_scope, 1000)}")
        if bot.system_prompt:
            prompt, _ = rules_backup.redact(clip(bot.system_prompt, PROMPT_CHARS))
            lines.append(f"- 提示词：\n{prompt}")
    out = Collected()
    unit = doc_unit(
        "roster", "roster", "roster", "平台名录", "\n".join(lines), cursor.get("roster"), today
    )
    if unit is not None:
        out.units.append(unit)
    return out


def render_task(
    row: WorkTask, *, requester: str, assignee: str, parent: str | None, closed: date
) -> str:
    lines = [f"## {row.code} {clip(row.title, 200)}", f"- 完成日期：{closed.isoformat()}"]
    if requester:
        lines.append(f"- 发起人：{requester}")
    if assignee:
        lines.append(f"- 执行：{assignee}")
    if parent:
        lines.append(f"- 上级任务：{parent}")
    if row.summary:
        lines.append(f"- 结果：{clip(row.summary, 500)}")
    for title, value, limit in (
        ("要求", row.brief, TASK_FIELD_CHARS),
        ("记要", row.digest, TASK_FIELD_CHARS),
        ("汇报", row.report, TASK_REPORT_CHARS),
    ):
        if value:
            lines.append(f"### {title}\n{clip(value, limit)}")
    return "\n".join(lines)


def task_mark(row: WorkTask) -> dict[str, Any]:
    assert row.closed_at is not None
    return {"closed_at": row.closed_at.isoformat(), "id": row.id}


async def tasks(session: AsyncSession, cursor: Mapping[str, Any]) -> Collected:
    """已完成的任务，按完成时间从进度位置往后。不够条件的跳过，进度照样往后走。"""
    stmt = select(WorkTask).where(
        WorkTask.status == "done",
        WorkTask.closed_at.is_not(None),
        WorkTask.source.in_(TASK_SOURCES),
    )
    if cursor.get("closed_at"):
        stmt = stmt.where(
            tuple_(WorkTask.closed_at, WorkTask.id)
            > (datetime.fromisoformat(cursor["closed_at"]), int(cursor.get("id") or 0))
        )
    rows = (
        (await session.execute(stmt.order_by(WorkTask.closed_at, WorkTask.id).limit(TASK_SCAN)))
        .scalars()
        .all()
    )
    out = Collected()
    if not rows:
        return out
    extra_ids = {
        value for row in rows for value in (row.root_id, row.parent_id) if value is not None
    } - {row.id for row in rows}
    related = {row.id: row for row in rows}
    if extra_ids:
        related.update(
            {
                row.id: row
                for row in (
                    await session.execute(select(WorkTask).where(WorkTask.id.in_(extra_ids)))
                ).scalars()
            }
        )
    bots = {
        row.id: row for row in (await session.execute(select(Bot.id, Bot.name, Bot.kind))).all()
    }
    chats = {row.chat_id for row in related.values() if row.chat_id}
    groups: set[str] = set()
    if chats:
        # 真的是多人群：留存期内的群消息里至少有两位不同的人说过话。私聊、单人群、认不出的都不算。
        groups = set(
            (
                await session.execute(
                    select(InboundEvent.chat_id)
                    .where(
                        InboundEvent.chat_id.in_(chats),
                        InboundEvent.chat_type == "group",
                        InboundEvent.kind == "message",
                        InboundEvent.sender_platform_user_id.is_not(None),
                    )
                    .group_by(InboundEvent.chat_id)
                    .having(func.count(func.distinct(InboundEvent.sender_platform_user_id)) >= 2)
                )
            ).scalars()
        )
    user_ids = {row.assignee_user_id for row in rows if row.assignee_user_id}
    users: dict[uuid.UUID | None, str] = {}
    if user_ids:
        for user_id, display_name in (
            await session.execute(select(User.id, User.display_name).where(User.id.in_(user_ids)))
        ).all():
            users[user_id] = display_name

    def standard(row: WorkTask) -> bool:
        involved = (row.assignee_bot_id, row.requester_bot_id, row.dispatcher_bot_id)
        return all(bots[i].kind == "standard" for i in involved if i is not None and i in bots)

    def open_origin(row: WorkTask) -> bool:
        """这件事是在大家都看得到的地方发起的：网页上建的，或多人群里调度出来的。"""
        if not standard(row):
            return False
        if row.source == "web":
            return True
        return row.source == "dispatch" and row.chat_id in groups

    def eligible(row: WorkTask) -> bool:
        if not standard(row):
            return False
        if row.source != "agent":
            return open_origin(row)
        root = related.get(row.root_id) if row.root_id is not None else None
        return root is not None and root.source != "agent" and open_origin(root)

    def name_of(bot_id: uuid.UUID | None) -> str:
        return f"AI 员工「{bots[bot_id].name}」" if bot_id in bots else ""

    pending: list[tuple[WorkTask, str]] = []
    size = 0

    def flush(mark_row: WorkTask) -> None:
        nonlocal pending, size
        batches: tuple[Batch, ...] = ()
        if pending:
            dates = {}
            for row, _ in pending:
                assert row.closed_at is not None
                dates[row.code] = row.closed_at.date()
            batches = (
                Batch(
                    source="task",
                    refs=tuple(row.code for row, _ in pending),
                    label="任务平台上已完成的任务",
                    text="\n\n".join(text for _, text in pending),
                    dates=dates,
                ),
            )
        out.units.append(Unit(source="task", key="task", mark=task_mark(mark_row), batches=batches))
        pending, size = [], 0

    last = rows[0]
    for row in rows:
        if not eligible(row):
            out.note("tasks_skipped")
            last = row
            continue
        assert row.closed_at is not None
        parent = related.get(row.parent_id) if row.parent_id is not None else None
        text = render_task(
            row,
            requester=name_of(row.requester_bot_id) or row.requester_name,
            assignee=name_of(row.assignee_bot_id) or users.get(row.assignee_user_id, ""),
            parent=parent.code if parent is not None else None,
            closed=row.closed_at.date(),
        )
        if pending and size + len(text) > BATCH_CHARS:
            flush(last)
        pending.append((row, text))
        size += len(text)
        last = row
    flush(last)
    return out
