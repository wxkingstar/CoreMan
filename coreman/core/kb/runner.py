"""知识库的一次运行：收集 → 抽取 → 规则过滤 → 入库 → 编译 → 编译后检查。

配置和进度都在 settings 表里（键以 `knowledge.` 开头）：承接员工、开关、额度阈值、每天几点跑、
各来源的进度位置、最近一次运行。运行记录在 `knowledge_runs`。

冷启动就是把进度清空后按同一套流程跑：批次上限更高，没跑完的后面每晚接着跑，跑完自动回到
日常上限。
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bots import rules_backup
from coreman.core.db.models import Bot, BusinessSystem, KnowledgeItem, KnowledgeRun
from coreman.core.kb import extract, filters, rounds, sources, wiki
from coreman.core.logging import get_logger
from coreman.core.relay.agent_client import AgentError
from coreman.core.settings_store import SettingsStore

log = get_logger(__name__)

BOT_KEY = "knowledge.bot_id"
ENABLED_KEY = "knowledge.enabled"
QUOTA_KEY = "knowledge.quota"
SCHEDULE_KEY = "knowledge.schedule"
COLD_KEY = "knowledge.cold_start"
STATE_KEY = "knowledge.last_run"
CURSOR_PREFIX = "knowledge.cursor."
DEFAULT_SCHEDULE = {"hour": 2, "timezone": "Asia/Shanghai"}
DAILY_BATCHES = 200
COLD_BATCHES = 500
# 每做这么多批重新看一次额度：超过阈值就换后端，都不够就停，把余量留给调度回合。
REPICK_EVERY = 10
MAX_RUN_SECONDS = 4 * 3600.0
# 一次运行卡死（进程被杀）后，过这么久才允许下一次开始。
STALE_RUNNING = timedelta(hours=6)
# 没跑成（额度不够、节点离线、出错）时隔多久再试，以及每天从约定时间起最多试到多久之后。
RETRY = timedelta(hours=1)
RETRY_WINDOW = timedelta(hours=6)
NOT_COMPILED = "not_compiled"
FIX_MAX_TURNS = 40


class KnowledgeError(Exception):
    """str() 是可以直接给管理员看的固定文案。"""


@dataclass(frozen=True)
class Config:
    bot_id: uuid.UUID | None
    enabled: bool
    quota: dict[str, Any]
    hour: int
    timezone: str
    cold_start: bool

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(DEFAULT_SCHEDULE["timezone"])  # type: ignore[arg-type]


async def load_config(store: SettingsStore) -> Config:
    raw = await store.get(BOT_KEY)
    try:
        bot_id = uuid.UUID(raw) if isinstance(raw, str) and raw else None
    except ValueError:
        bot_id = None
    quota = await store.get(QUOTA_KEY)
    schedule = await store.get(SCHEDULE_KEY)
    schedule = {**DEFAULT_SCHEDULE, **(schedule if isinstance(schedule, dict) else {})}
    hour = schedule["hour"] if isinstance(schedule["hour"], int) else DEFAULT_SCHEDULE["hour"]
    return Config(
        bot_id=bot_id,
        enabled=await store.get(ENABLED_KEY) is True,
        quota={**rounds.DEFAULT_QUOTA, **(quota if isinstance(quota, dict) else {})},
        hour=min(max(int(hour), 0), 23),  # type: ignore[call-overload]
        timezone=str(schedule["timezone"]),
        cold_start=await store.get(COLD_KEY) is True,
    )


def usable(bot: Bot | None) -> str | None:
    """这个员工现在能不能承接知识库；不能时返回原因。"""
    if bot is None:
        return "还没有指定承接知识库的 AI 员工"
    if bot.kind != "standard" or bot.group_role != "dispatcher":
        return "承接知识库的 AI 员工必须是调度员"
    if not bot.enabled:
        return "承接知识库的 AI 员工已停用"
    if not bot.working_dir or bot.workspace_state != "ready":
        return "承接知识库的 AI 员工工作目录还没有就绪"
    return None


async def configure(
    factory: async_sessionmaker[AsyncSession],
    store: SettingsStore,
    *,
    bot_key: str | None = None,
    enabled: bool | None = None,
    hour: int | None = None,
    timezone: str | None = None,
    seven_day: int | None = None,
    five_hour: int | None = None,
) -> None:
    """改知识库的设置；只改给了值的项。"""
    if bot_key is not None:
        async with factory() as session:
            bot = (
                await session.execute(select(Bot).where(Bot.bot_key == bot_key))
            ).scalar_one_or_none()
        if bot is None:
            raise KnowledgeError(f"没有标识为 {bot_key} 的 AI 员工")
        if (reason := usable(bot)) is not None:
            raise KnowledgeError(reason)
        await store.set(BOT_KEY, str(bot.id))
    if hour is not None or timezone is not None:
        current = await load_config(store)
        if hour is not None and not 0 <= hour <= 23:
            raise KnowledgeError("小时要在 0 到 23 之间")
        if timezone is not None:
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise KnowledgeError(f"不认识的时区：{timezone}") from exc
        await store.set(
            SCHEDULE_KEY,
            {
                "hour": current.hour if hour is None else hour,
                "timezone": current.timezone if timezone is None else timezone,
            },
        )
    if seven_day is not None or five_hour is not None:
        quota = dict((await load_config(store)).quota)
        for name, value in (("seven_day", seven_day), ("five_hour", five_hour)):
            if value is not None:
                if not 1 <= value <= 100:
                    raise KnowledgeError("额度阈值要在 1 到 100 之间")
                quota[name] = value
        await store.set(QUOTA_KEY, quota)
    if enabled is not None:
        if enabled and (await load_config(store)).bot_id is None:
            raise KnowledgeError("先指定承接知识库的 AI 员工，再开启")
        await store.set(ENABLED_KEY, enabled)


async def cursors(store: SettingsStore) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for source in sources.SOURCES:
        value = await store.get(CURSOR_PREFIX + source)
        out[source] = dict(value) if isinstance(value, dict) else {}
    return out


async def collect(
    factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    cursor: dict[str, dict[str, Any]],
    today: Any,
) -> sources.Collected:
    """按顺序收集各来源这次要处理的单元：先结构化的来源搭骨架，任务在后。"""
    out = sources.Collected()
    parts = [await sources.rules(factory, bot, cursor["rules"], today)]
    async with factory() as session:
        parts.append(await sources.catalog(session, cursor["catalog"], today))
        parts.append(await sources.roster(session, bot, cursor["roster"], today))
        parts.append(await sources.tasks(session, cursor["task"]))
        session.expunge_all()
    for part in parts:
        out.units.extend(part.units)
        for name, count in part.notes.items():
            out.note(name, count)
    return out


def planned(units: list[sources.Unit]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for unit in units:
        if unit.batches:
            row = out.setdefault(unit.source, {"units": 0, "batches": 0})
            row["units"] += 1
            row["batches"] += len(unit.batches)
    return out


async def _round(backend: rounds.Backend, **kwargs: Any) -> str:
    """跑一轮；节点或驱动出错时再试一次。"""
    try:
        return await rounds.run_round(backend, **kwargs)
    except rounds.RoundError as exc:
        log.warning("kb_round_retry", provider=backend.provider, error=str(exc))
        return await rounds.run_round(backend, **kwargs)


async def extract_batch(backend: rounds.Backend, batch: sources.Batch) -> list[extract.Draft]:
    """抽一批；输出不是约定格式时再抽一次，仍不是就抛 ParseError。"""
    for attempt in range(2):
        output = await _round(
            backend,
            system_prompt=extract.SYSTEM_PROMPT,
            content=extract.content(batch),
            working_dir=extract.SCRATCH_DIR,
            max_turns=extract.MAX_TURNS,
            limit_seconds=extract.TIMEOUT_SECONDS,
        )
        try:
            return extract.parse(output, batch)
        except extract.ParseError:
            if attempt:
                raise
    raise extract.ParseError("unreachable")


class _Run:
    """一次运行里边走边记的数。"""

    def __init__(self) -> None:
        self.batches = 0
        self.failed_batches = 0
        self.extracted = 0
        self.dropped = 0
        self.compiled = 0
        self.reasons: Counter[str] = Counter()
        self.hits: list[dict[str, Any]] = []
        self.summaries: list[str] = []
        self.more = False
        self.stopped: str | None = None
        self.started = time.monotonic()

    def out_of_time(self) -> bool:
        return time.monotonic() - self.started > MAX_RUN_SECONDS


async def _extract(
    factory: async_sessionmaker[AsyncSession],
    store: SettingsStore,
    bot: Bot,
    backend: rounds.Backend,
    units: list[sources.Unit],
    cursor: dict[str, dict[str, Any]],
    quota: dict[str, Any],
    run_id: int,
    cap: int,
    state: _Run,
) -> rounds.Backend:
    """逐个单元抽取、过滤、入库并推进进度；返回最后在用的后端。"""
    for unit in units:
        if unit.batches and (state.batches >= cap or state.out_of_time()):
            state.more = True
            break
        drafts: list[extract.Draft] = []
        for batch in unit.batches:
            if state.batches and state.batches % REPICK_EVERY == 0:
                async with factory() as session:
                    picked, reason = await rounds.pick(session, bot, quota, datetime.now(UTC))
                    session.expunge_all()
                if picked is None:
                    state.more, state.stopped = True, reason
                    return backend
                backend = picked
            state.batches += 1
            try:
                drafts.extend(await extract_batch(backend, batch))
            except extract.ParseError:
                state.failed_batches += 1
                log.warning("kb_extract_unparsed", source=batch.source, refs=list(batch.refs)[:5])
        async with factory() as session:
            for draft in drafts:
                rule = filters.hit(f"{draft.topic}\n{draft.text}")
                session.add(
                    KnowledgeItem(
                        bot_id=bot.id,
                        kind=draft.kind,
                        topic=draft.topic,
                        # 命中规则的条目不留正文：留着就等于把该丢的东西存了下来。
                        text="" if rule else draft.text,
                        occurred_on=draft.occurred_on,
                        confidence=draft.confidence,
                        source_kind=unit.source,
                        source_ref=draft.source_ref,
                        status="dropped" if rule else "pending",
                        dropped_reason=rule,
                        run_id=run_id,
                    )
                )
                state.extracted += 1
                if rule:
                    state.dropped += 1
                    state.reasons[rule] += 1
            await session.commit()
        if unit.source == "task":
            cursor["task"] = dict(unit.mark)
        else:
            cursor[unit.source][unit.key] = unit.mark
        await store.set(CURSOR_PREFIX + unit.source, cursor[unit.source])
    return backend


async def _compile(
    factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    backend: rounds.Backend,
    today: str,
    state: _Run,
) -> None:
    """把还没编译的条目分批并进 Wiki，每批之后做一次检查。"""
    async with factory() as session:
        pending = list(
            (
                await session.execute(
                    select(KnowledgeItem)
                    .where(KnowledgeItem.bot_id == bot.id, KnowledgeItem.status == "pending")
                    .order_by(KnowledgeItem.id)
                )
            ).scalars()
        )
        names = {
            f"rules:{key}": name
            for key, name in (await session.execute(select(Bot.bot_key, Bot.name))).all()
        }
        names.update(
            {
                f"catalog:{key}": name
                for key, name in (
                    await session.execute(select(BusinessSystem.key, BusinessSystem.name))
                ).all()
            }
        )
        session.expunge_all()
    if not pending:
        return
    before = await wiki.read_tree(backend.relay, bot)
    for start in range(0, len(pending), wiki.CHUNK):
        if state.out_of_time():
            state.more = True
            return
        chunk = pending[start : start + wiki.CHUNK]
        output = await _round(
            backend,
            system_prompt=wiki.SYSTEM_PROMPT,
            content=wiki.content(chunk, today, names),
            working_dir=bot.working_dir,
            max_turns=wiki.MAX_TURNS,
            limit_seconds=wiki.TIMEOUT_SECONDS,
        )
        if text := wiki.summary(output):
            state.summaries.append(text)
        after = await wiki.read_tree(backend.relay, bot)
        hits = wiki.check(before, after)
        for _ in range(wiki.MAX_FIXES):
            if not hits:
                break
            await _round(
                backend,
                system_prompt=wiki.FIX_PROMPT,
                content=wiki.fix_content(hits),
                working_dir=bot.working_dir,
                max_turns=FIX_MAX_TURNS,
                limit_seconds=wiki.FIX_TIMEOUT_SECONDS,
            )
            after = await wiki.read_tree(backend.relay, bot)
            hits = wiki.check(before, after)
        state.hits.extend({"path": h.path, "line": h.line, "rule": h.rule} for h in hits)
        present = wiki.cited(after)
        now = datetime.now(UTC)
        async with factory() as session:
            rows = (
                await session.execute(
                    select(KnowledgeItem).where(KnowledgeItem.id.in_([item.id for item in chunk]))
                )
            ).scalars()
            for row in rows:
                row.attempts += 1
                if row.id in present:
                    row.status, row.compiled_at = "compiled", now
                    state.compiled += 1
                elif row.attempts >= wiki.MAX_ATTEMPTS:
                    # 几次都没落页：多半和已有内容重复或被判断为不该写，不再重试。
                    row.status, row.dropped_reason = "dropped", NOT_COMPILED
            await session.commit()
        before = after


async def run(
    factory: async_sessionmaker[AsyncSession],
    store: SettingsStore,
    now: datetime,
    *,
    apply: bool = True,
    cold_start: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """跑一次，返回可以写进状态与日志的摘要（不含材料和条目正文）。

    `apply=False` 只看这次会读哪些来源、多少批，不调用模型、不写任何东西。`cold_start` 把进度
    清空后从头来。

    Raises:
        KnowledgeError: 没有可用的承接员工，或已有一次运行在进行
    """
    config = await load_config(store)
    async with factory() as session:
        bot = await session.get(Bot, config.bot_id) if config.bot_id else None
        if (reason := usable(bot)) is not None:
            raise KnowledgeError(reason)
        assert bot is not None
        backend, why = await rounds.pick(session, bot, config.quota, now)
        busy = (
            await session.execute(
                select(KnowledgeRun.id).where(
                    KnowledgeRun.status == "running",
                    KnowledgeRun.started_at > now - STALE_RUNNING,
                )
            )
        ).first()
        waiting = (
            await session.execute(
                select(func.count())
                .select_from(KnowledgeItem)
                .where(KnowledgeItem.bot_id == bot.id, KnowledgeItem.status == "pending")
            )
        ).scalar_one()
        session.expunge_all()
    today = now.astimezone(config.zone).date()
    cursor = {source: {} for source in sources.SOURCES} if cold_start else await cursors(store)
    cold = cold_start or config.cold_start
    cap = limit or (COLD_BATCHES if cold else DAILY_BATCHES)
    found = await collect(factory, bot, cursor, today)
    report: dict[str, Any] = {
        "at": now.isoformat(),
        "mode": "cold_start" if cold else "daily",
        "bot_key": bot.bot_key,
        "provider": backend.provider if backend else None,
        "model": backend.model if backend else None,
        "sources": planned(found.units),
        "notes": found.notes,
        "batch_limit": cap,
    }
    if not apply:
        return {**report, "preview": True, "pending_items": waiting, "skip_reason": why or None}
    if busy is not None:
        raise KnowledgeError("已有一次知识库运行在进行，等它结束再试")
    if cold_start:
        for source in sources.SOURCES:
            await store.set(CURSOR_PREFIX + source, {})
        await store.set(COLD_KEY, True)
    async with factory() as session:
        row = KnowledgeRun(
            bot_id=bot.id,
            mode=report["mode"],
            status="running" if backend else "skipped",
            provider=report["provider"],
            model=report["model"],
            error=None if backend else why,
            finished_at=None if backend else now,
        )
        session.add(row)
        await session.commit()
        run_id = row.id
    if backend is None:
        report.update(status="skipped", error=why)
        await store.set(STATE_KEY, report)
        return report
    state = _Run()
    status: str = "ok"
    error: str | None = None
    crash: Exception | None = None
    try:
        backend = await _extract(
            factory, store, bot, backend, found.units, cursor, config.quota, run_id, cap, state
        )
        if state.stopped is None:
            await _compile(factory, bot, backend, today.isoformat(), state)
    except (rounds.RoundError, AgentError, rules_backup.BackupError) as exc:
        status, error = "failed", str(exc) or type(exc).__name__
        if isinstance(exc, AgentError) and exc.detail:
            error = f"{error}：{exc.detail}"
        state.more = True
    except Exception as exc:
        # 没料到的错：照样把这次运行收尾（否则它一直是「进行中」，挡住后面的运行），再抛出去。
        status, error, crash = "failed", type(exc).__name__, exc
        state.more = True
    if status == "ok" and state.hits:
        status = "needs_review"
    if status != "failed" and state.stopped:
        error = f"中途停下：{state.stopped}"
    if cold and not state.more and status != "failed":
        await store.set(COLD_KEY, False)
    report.update(
        status=status,
        error=error,
        batches=state.batches,
        failed_batches=state.failed_batches,
        extracted=state.extracted,
        dropped=state.dropped,
        compiled=state.compiled,
        more=state.more,
        dropped_reasons=dict(state.reasons),
        hits=state.hits[:50],
    )
    async with factory() as session:
        saved = await session.get(KnowledgeRun, run_id)
        assert saved is not None
        saved.status, saved.error, saved.finished_at = status, error, datetime.now(UTC)
        saved.provider, saved.model = backend.provider, backend.model
        saved.batches, saved.failed_batches = state.batches, state.failed_batches
        saved.extracted, saved.dropped, saved.compiled = (
            state.extracted,
            state.dropped,
            state.compiled,
        )
        saved.summary = "\n\n".join(state.summaries)[: 4 * wiki.SUMMARY_CHARS]
        saved.detail = {
            key: report[key] for key in ("sources", "notes", "dropped_reasons", "hits", "more")
        }
        await session.commit()
    await store.set(STATE_KEY, report)
    if crash is not None:
        raise crash
    return report


def due(config: Config, last: dict[str, Any], now: datetime) -> bool:
    """现在该不该跑：每天约定时间之后跑一次；没跑成的，在之后几小时里每小时再试。"""
    local = now.astimezone(config.zone)
    slot = local.replace(hour=config.hour, minute=0, second=0, microsecond=0)
    if local < slot:
        slot -= timedelta(days=1)
    try:
        at = datetime.fromisoformat(last["at"]) if last.get("at") else None
    except (TypeError, ValueError):
        at = None
    if at is None or at < slot:
        return True
    if last.get("status") in ("failed", "skipped"):
        return now - at >= RETRY and now - slot <= RETRY_WINDOW
    return False


async def run_due(
    factory: async_sessionmaker[AsyncSession], store: SettingsStore, now: datetime
) -> dict[str, Any] | None:
    """开着且到点了就跑一次并记下结果；没开或没到点返回 None。"""
    config = await load_config(store)
    if not config.enabled or config.bot_id is None:
        return None
    last = await store.get(STATE_KEY)
    if not due(config, last if isinstance(last, dict) else {}, now):
        return None
    try:
        return await run(factory, store, now)
    except KnowledgeError as exc:
        report = {"at": now.isoformat(), "status": "failed", "error": str(exc)}
        await store.set(STATE_KEY, report)
        return report


async def status(factory: async_sessionmaker[AsyncSession], store: SettingsStore) -> dict[str, Any]:
    """设置、条目计数和最近几次运行，给管理员看。"""
    config = await load_config(store)
    async with factory() as session:
        bot = await session.get(Bot, config.bot_id) if config.bot_id else None
        counts: dict[str, int] = {}
        for name, count in (
            await session.execute(
                select(KnowledgeItem.status, func.count()).group_by(KnowledgeItem.status)
            )
        ).all():
            counts[name] = count
        runs = (
            (await session.execute(select(KnowledgeRun).order_by(KnowledgeRun.id.desc()).limit(5)))
            .scalars()
            .all()
        )
        recent = [
            {
                "id": row.id,
                "mode": row.mode,
                "status": row.status,
                "provider": row.provider,
                "started_at": row.started_at.isoformat(),
                "batches": row.batches,
                "extracted": row.extracted,
                "dropped": row.dropped,
                "compiled": row.compiled,
                "error": row.error,
            }
            for row in runs
        ]
    return {
        "enabled": config.enabled,
        "bot_key": bot.bot_key if bot else None,
        "bot_problem": usable(bot),
        "schedule": {"hour": config.hour, "timezone": config.timezone},
        "quota": config.quota,
        "cold_start": config.cold_start,
        "items": counts,
        "runs": recent,
        "last_run": await store.get(STATE_KEY),
    }
