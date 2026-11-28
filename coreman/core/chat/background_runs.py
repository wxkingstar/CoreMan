"""Claude 后台阶段（`background_runs`）：主回合结束时还有后台任务，进程不退出，由平台看着它做完。

一段后台阶段的生命周期：

- 进入：chat 任务的流里收到 `x_runtime.background`，按成功收尾，写一行 `running`，再建一个
  `background_watch` 任务接上节点的 `/live/attach`。
- 期间：后台任务每完成一项，CLI 续出一个回合，观察任务把它作为新消息回复在原消息下并 @ 提问人；
  提问人接着发的消息写进同一个进程（不重新签发凭据）；群里别人发的消息排队等它结束。
- 结束：驱动报 `background_end`（全部做完、被停、到期、进程异常），或平台判定节点丢失。

所有对节点的调用（attach、stop_task、close）都在 worker 里经运行时反向通道发出：网关、看护进程
与管理接口要停一段后台阶段时，入队一个 `background_control` 任务。

只有 Claude 运行时、飞书上的普通员工、节点声明了 `claude_background_sessions_v1` 才走这条路；
企业微信与个人助理不启用（企微的回复窗口与卡片更新方式不同，见 docs/feishu.md）。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.crypto import Cipher
from coreman.core.db.models import BackgroundRun, RelayServer, RuntimeNode, Task

CAPABILITY = "claude_background_sessions_v1"
WATCH_KIND = "background_watch"
CONTROL_KIND = "background_control"
# 卡片上的「停止」与「停掉全部后台任务」：value 的 task_id 是 `bg@<run id>`，`bg_task` 是后台
# 任务 id，`*` 表示全部。
CARD_PREFIX = "bg@"
EVENT_KEY = "background_stop"
ALL = "*"
_CARD_ID = re.compile(r"bg@([1-9][0-9]{0,18})")
# 一项这么久没有任何进度：卡片上标「好久没有进展」，不判卡住（后台常跑长测试、长构建）。
STALL_SECONDS = 10 * 60
# 卡片区块刷新的最小间隔；有任务完成、停止这类状态变化时立即刷新。
CARD_INTERVAL_SECONDS = 30
# 节点离线、或心跳里持续不见这个会话这么久，判为丢失。
LOST_GRACE_SECONDS = 120
# 群里别人发来的消息排队等后台结束；到期后再留这么久兜底放行（正常由收尾时放行）。
HOLD_MARGIN_SECONDS = 5 * 60
CLOSE_REASONS = ("user", "expired", "reset")
# 本人发起、续的是同一个会话的轮次：后台阶段里可以写进同一个进程（问卷答案、凭证与审批的续接）。
OWNER_KINDS = frozenset({"chat", "choice_submit", "credential_resume", "approval_resume"})
# 收尾时 close 原因对应的终态。
CLOSE_STATES = {"user": "stopped", "reset": "stopped", "expired": "expired"}
TERMINAL = ("finished", "stopped", "expired", "lost")


def card_task_id(run_id: int) -> str:
    return f"{CARD_PREFIX}{run_id}"


def parse_card_task_id(value: str) -> int | None:
    match = _CARD_ID.fullmatch(value)
    return int(match.group(1)) if match else None


async def supported(session: AsyncSession, relay: RelayServer | None, backend: str) -> bool:
    """员工所在节点的 Claude 驱动能不能进后台阶段。"""
    if backend != "claude" or relay is None or relay.runtime_node_id is None:
        return False
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    capability = ((node.capabilities if node else None) or {}).get("claude") or {}
    return isinstance(capability, dict) and capability.get(CAPABILITY) is True


async def running_for(
    session: AsyncSession, bot_id: uuid.UUID, session_key: str, *, lock: bool = False
) -> BackgroundRun | None:
    stmt = select(BackgroundRun).where(
        BackgroundRun.bot_id == bot_id,
        BackgroundRun.session_key == session_key,
        BackgroundRun.state == "running",
    )
    if lock:
        stmt = stmt.with_for_update()
    row: BackgroundRun | None = await session.scalar(stmt.execution_options(populate_existing=True))
    return row


def running(tasks_: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in tasks_ if t.get("status") == "running"]


def merge(old: Iterable[dict[str, Any]], new: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 id 合并快照：新的覆盖旧的，旧快照里有、新快照里没有的保留（已结束的项驱动可能不再列）。

    顺序按第一次出现排，卡片上的编号 ①②③ 不会因为后来的快照换位。
    """
    merged: dict[str, dict[str, Any]] = {}
    for item in old:
        merged[str(item.get("id"))] = dict(item)
    for item in new:
        key = str(item.get("id"))
        merged[key] = {**merged.get(key, {}), **item}
    return list(merged.values())


def finished_since(
    old: Iterable[dict[str, Any]], new: Iterable[dict[str, Any]]
) -> list[tuple[int, dict[str, Any]]]:
    """新快照里由 running 变成已结束的项，带上在合并快照里的序号（从 1 起）。"""
    before = {str(t.get("id")): t.get("status") for t in old}
    merged = merge(old, new)
    index = {str(t.get("id")): i for i, t in enumerate(merged, 1)}
    out = []
    for item in new:
        key = str(item.get("id"))
        if item.get("status") != "running" and before.get(key, "running") == "running":
            out.append((index[key], item))
    return out


NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def number(index: int) -> str:
    return NUMBERS[index - 1] if 1 <= index <= len(NUMBERS) else f"({index})"


def minutes(seconds: float) -> str:
    total = max(int(seconds // 60), 0)
    if total < 1:
        return "不到 1 分钟"
    hours, mins = divmod(total, 60)
    return f"{hours} 小时 {mins} 分钟" if hours else f"{mins} 分钟"


STATUS_TEXT = {"completed": "已完成 ✓", "stopped": "已停止", "failed": "失败"}


def task_line(index: int, task: dict[str, Any], now: datetime) -> str:
    """卡片与通知里的一行：「① 订单导出接口  进行中 12 分钟 · 最近：跑后端测试」。"""
    name = task.get("description") or "后台任务"
    head = f"{number(index)} {name}"
    status = task.get("status")
    if status != "running":
        return f"{head}　{STATUS_TEXT.get(str(status), str(status))}"
    started = task.get("started_at")
    ran = now.timestamp() - started if isinstance(started, int) else 0
    line = f"{head}　进行中 {minutes(ran)}"
    last = task.get("last_progress_at") or started
    if isinstance(last, int) and now.timestamp() - last >= STALL_SECONDS:
        return f"{line} · 好久没有进展"
    if task.get("last_progress"):
        line += f" · 最近：{task['last_progress']}"
    return line


def unfinished_lines(run: BackgroundRun, now: datetime) -> list[str]:
    return [
        task_line(i, t, now)
        for i, t in enumerate(run.tasks or [], 1)
        if t.get("status") in ("running", "stopped", "failed")
    ]


def deadline_text(deadline: datetime) -> str:
    """卡片上的「最长可跑到 16:40」：飞书的 local_datetime 按看的人设备时区显示。"""
    ms = int(deadline.timestamp() * 1000)
    return f"<local_datetime millisecond='{ms}' format_type='date_num'></local_datetime>"


def heading(run_state: str, tasks_: list[dict[str, Any]]) -> str:
    """主回合卡片的标题行（思考面板标题）。"""
    if run_state == "running":
        return f"⏳ 已开工 · 后台 {len(running(tasks_))} 项进行中"
    if run_state == "finished":
        return "✅ 后台全部完成"
    if run_state == "expired":
        return "⏹ 后台任务已到时限"
    if run_state == "lost":
        return "❌ 后台任务因节点中断停止"
    return "⏹ 后台任务已停止"


def view(run: BackgroundRun) -> dict[str, Any]:
    """网关渲染卡片区块要的全部数据（写进 task_streams.activity 与出站条目）。"""
    return {
        "run_id": run.id,
        "state": run.state,
        "tasks": list(run.tasks or []),
        "deadline": int(run.deadline.timestamp()),
    }


def seal_secrets(cipher: Cipher, run: BackgroundRun, secrets: Iterable[str]) -> str | None:
    values = sorted(set(secrets))
    if not values:
        return None
    return cipher.encrypt(json.dumps(values), _secrets_aad(run))


def open_secrets(cipher: Cipher, run: BackgroundRun) -> frozenset[str]:
    if not run.secrets_enc:
        return frozenset()
    try:
        values = json.loads(cipher.decrypt(run.secrets_enc, _secrets_aad(run)))
    except Exception:  # noqa: BLE001 解不开就按没有处理，出站仍按已知规则过滤
        return frozenset()
    return frozenset(v for v in values if isinstance(v, str))


def _secrets_aad(run: BackgroundRun) -> str:
    return f"background_run:{run.bot_id}:{run.relay_session_id}:{run.origin_task_id}"


async def enqueue_watch(session: AsyncSession, run: BackgroundRun, *, generation: int = 0) -> None:
    """建观察任务。不带 session_key：它不参与会话串行，也不会被新消息替代。"""
    await tasks.enqueue(
        session,
        NewTask(
            bot_id=run.bot_id,
            kind=WATCH_KIND,
            payload={"run_id": run.id, "generation": generation},
            dedupe_key=f"bg-watch:{run.id}:{generation}",
        ),
    )


async def watch_generation(session: AsyncSession, run_id: int) -> int:
    """这一段后台阶段已经建过几个观察任务（重建时用下一个编号）。"""
    from coreman.core.db.models import Task

    count = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.kind == WATCH_KIND, Task.payload["run_id"].astext == str(run_id))
    )
    return int(count or 0)


async def has_open_watch(session: AsyncSession, run_id: int) -> bool:
    from coreman.core.db.models import Task

    found = await session.scalar(
        select(Task.id)
        .where(
            Task.kind == WATCH_KIND,
            Task.status.in_(tasks.OPEN),
            Task.payload["run_id"].astext == str(run_id),
        )
        .limit(1)
    )
    return found is not None


async def request_close(session: AsyncSession, run: BackgroundRun, reason: str) -> bool:
    """请求关掉整段后台阶段：记下原因，入队控制任务由 worker 调节点的 `/live/close`。

    已经请求过的不再请求（原因以第一次为准）。返回这次是否新发起。调用方持有行锁。
    """
    if reason not in CLOSE_REASONS:
        raise ValueError(reason)
    if run.state != "running" or run.close_requested_at is not None:
        return False
    run.close_reason, run.close_requested_at = reason, datetime.now(UTC)
    run.updated_at = datetime.now(UTC)
    await session.flush()
    await tasks.enqueue(
        session,
        NewTask(
            bot_id=run.bot_id,
            kind=CONTROL_KIND,
            lane="fast",
            payload={"run_id": run.id, "action": "close", "reason": reason},
            dedupe_key=f"bg-close:{run.id}",
        ),
    )
    return True


async def request_stop_task(session: AsyncSession, run: BackgroundRun, task_id: str) -> bool:
    """停一项后台任务（`/live/stop_task`），其余照跑。"""
    if run.state != "running" or not any(
        t.get("id") == task_id and t.get("status") == "running" for t in run.tasks or []
    ):
        return False
    created = await tasks.enqueue(
        session,
        NewTask(
            bot_id=run.bot_id,
            kind=CONTROL_KIND,
            lane="fast",
            payload={"run_id": run.id, "action": "stop_task", "task_id": task_id},
            dedupe_key=f"bg-stop:{run.id}:{task_id}",
        ),
    )
    return created is not None


async def close_for_session(
    session: AsyncSession, bot_id: uuid.UUID, session_key: str, reason: str
) -> BackgroundRun | None:
    """重置会话前：这个会话在后台阶段就关掉，状态立刻定为终态（之后的新消息起新进程）。"""
    run = await running_for(session, bot_id, session_key, lock=True)
    if run is None:
        return None
    await request_close(session, run, reason)
    await end(session, run, CLOSE_STATES[reason])
    return run


async def close_for_bot(session: AsyncSession, bot_id: uuid.UUID, reason: str = "reset") -> int:
    """换运行时、换模型、换工作目录前：这个员工所有在后台阶段的会话都关掉。"""
    rows = list(
        await session.scalars(
            select(BackgroundRun)
            .where(BackgroundRun.bot_id == bot_id, BackgroundRun.state == "running")
            .with_for_update()
        )
    )
    for run in rows:
        await request_close(session, run, reason)
        await end(session, run, CLOSE_STATES[reason])
    return len(rows)


async def end(session: AsyncSession, run: BackgroundRun, state: str) -> bool:
    """写终态并放行排队等它的消息；已经是终态的不改，返回是否这次写的。调用方持有行锁。"""
    if state not in TERMINAL:
        raise ValueError(state)
    if run.state != "running":
        return False
    moment = datetime.now(UTC)
    run.state, run.ended_at, run.updated_at = state, moment, moment
    await session.flush()
    await release_held(session, run)
    return True


async def release_held(session: AsyncSession, run: BackgroundRun) -> int:
    """群里别人等后台结束的消息：立刻可认领。"""
    from coreman.core.db.models import Task

    ids = list(
        await session.scalars(
            update(Task)
            .where(
                Task.bot_id == run.bot_id,
                Task.status == "queued",
                Task.payload["background_hold"].astext == str(run.id),
            )
            .values(
                run_after=func.now(),
                payload=Task.payload.op("-")(text("'background_hold'")),
            )
            .returning(Task.id)
        )
    )
    if ids:
        from coreman.core.bus.notify import notify

        await notify(session, "tasks_queued", {"lane": "normal", "bot_id": str(run.bot_id)})
    return len(ids)


async def origin_text(session: AsyncSession, run: BackgroundRun) -> str:
    """开工那一轮提问的正文；任务已不在或没有文字时返回空串。"""
    task = await session.get(Task, run.origin_task_id)
    message = (task.payload if task is not None else {}).get("message")
    parts = message.get("parts") if isinstance(message, dict) else None
    return " ".join(
        str(p.get("text") or "")
        for p in parts or []
        if isinstance(p, dict) and p.get("type") == "text"
    ).strip()


def reply_target(run: BackgroundRun) -> dict[str, Any]:
    """回复在开工那一轮的原消息下。"""
    target: dict[str, Any] = {"chat_id": run.chat_id}
    if run.origin_message_id:
        target["message_id"] = run.origin_message_id
    return target


async def queue_card(session: AsyncSession, run: BackgroundRun, key: str) -> None:
    """刷新开工那一轮卡片上的后台区块与标题（飞书网关按 `_background_card` 处理）。"""
    run.card_updated_at = datetime.now(UTC)
    task_id = (run.card_ref or {}).get("task_id")
    if run.platform != "feishu" or not isinstance(task_id, int):
        return
    from coreman.core.bus import outbox

    await outbox.add(
        session,
        bot_id=run.bot_id,
        platform=run.platform,
        kind="send",
        dedupe_key=f"bg-card:{run.id}:{key}",
        target={"chat_id": run.chat_id},
        payload={"_background_card": {"task_id": task_id, **view(run)}},
    )


async def queue_notice(session: AsyncSession, run: BackgroundRun, locale: str = "zh") -> None:
    """被停、到期、丢失：主动告诉提问人（回复在原消息下并 @ 他），列出没做完的和已跑时长。"""
    from coreman.core.bus import outbox
    from coreman.core.i18n.messages import msg

    now = datetime.now(UTC)
    elapsed = minutes((now - run.created_at).total_seconds())
    lines = "\n".join(unfinished_lines(run, now)) or "（无）"
    if run.state == "lost":
        text = msg("background_lost_notice", locale, elapsed=elapsed)
    elif run.state == "expired":
        text = msg("background_expired_notice", locale, elapsed=elapsed, lines=lines)
    else:
        text = msg("background_stopped_notice", locale, elapsed=elapsed, lines=lines)
    await outbox.add(
        session,
        bot_id=run.bot_id,
        platform=run.platform,
        kind="send",
        dedupe_key=f"bg-end:{run.id}",
        target=reply_target(run),
        payload={"markdown": text, "_mention_user_id": run.owner_platform_user_id},
    )


async def conclude(session: AsyncSession, run: BackgroundRun, state: str, key: str) -> bool:
    """平台替驱动收尾（丢失、close 迟迟没有回音）：还在跑的项记为停止，定终态、刷卡片、告知。"""
    if state != "finished":
        run.tasks = [
            {**t, "status": "stopped"} if t.get("status") == "running" else t
            for t in run.tasks or []
        ]
    ended = await end(session, run, state)
    if ended:
        await queue_card(session, run, key)
        if state != "finished":
            await queue_notice(session, run)
    return ended


REPLACED = "replaced"


async def replace(session: AsyncSession, run: BackgroundRun) -> None:
    """驱动说进程已经不在（not_live），本人的新消息要起新进程：这一段记为结束、腾出位置。

    观察任务还连着的话照常收完正在读的那条流（驱动的墓碑会补发缓存的回合和结束帧），只是不再
    重连：同一会话的新进程归下一段后台阶段的观察任务。调用方持有行锁。
    """
    run.close_reason = REPLACED
    await end(session, run, "finished")
    await queue_card(session, run, "replaced")


def overdue(run: BackgroundRun, now: datetime) -> bool:
    """到期之后驱动兜底的 close 也该做完了，仍是 running：平台按到期收尾。"""
    return now >= run.deadline + timedelta(seconds=EXPIRE_GRACE_SECONDS)


def hold_until(run: BackgroundRun) -> datetime:
    return run.deadline + timedelta(seconds=HOLD_MARGIN_SECONDS)


# close 发出后这么久驱动还没收尾（控制任务失败、节点卡住）：平台按请求的原因直接收尾。
CLOSE_GRACE_SECONDS = 180
# 到期之后驱动兜底的 close 也该做完了：再过这么久仍是 running 就按到期收尾。
EXPIRE_GRACE_SECONDS = 600


async def tick(session: AsyncSession, now: datetime) -> dict[str, int]:
    """看护：到期的发 close；close 迟迟没回音、节点离线超过 2 分钟的由平台收尾；
    没有观察任务的补一个。

    「心跳里不再列出这个会话」由观察任务判定：它 attach 得到 404、节点心跳也不列时记为丢失。
    """
    counts = {"background_expired": 0, "background_concluded": 0, "background_rewatched": 0}
    rows = list(
        await session.scalars(
            select(BackgroundRun)
            .where(BackgroundRun.state == "running")
            .order_by(BackgroundRun.id)
            .limit(200)
            .with_for_update(skip_locked=True)
        )
    )
    key = f"tick:{int(now.timestamp())}"
    for run in rows:
        if run.close_requested_at is not None:
            if (now - run.close_requested_at).total_seconds() >= CLOSE_GRACE_SECONDS:
                state = CLOSE_STATES.get(run.close_reason or "", "stopped")
                counts["background_concluded"] += await conclude(session, run, state, key)
                continue
        elif now >= run.deadline:
            counts["background_expired"] += await request_close(session, run, "expired")
        if overdue(run, now):
            counts["background_concluded"] += await conclude(session, run, "expired", key)
            continue
        node = await session.get(RuntimeNode, run.node_id) if run.node_id else None
        if node is None or not node.is_active or _offline(node, now):
            counts["background_concluded"] += await conclude(session, run, "lost", key)
            continue
        if not await has_open_watch(session, run.id):
            await enqueue_watch(session, run, generation=await watch_generation(session, run.id))
            counts["background_rewatched"] += 1
    return counts


def _offline(node: RuntimeNode, now: datetime) -> bool:
    if node.heartbeat_at is None:
        return True
    return (now - node.heartbeat_at).total_seconds() >= LOST_GRACE_SECONDS + 45


async def can_stop(session: AsyncSession, run: BackgroundRun, platform_user_id: str) -> bool:
    """卡片上的「停止」「停掉全部后台任务」：开工那一轮的提问人与员工管理员能点。"""
    if not platform_user_id:
        return False
    if platform_user_id == run.owner_platform_user_id:
        return True
    from coreman.core.bots.permissions import is_bot_admin
    from coreman.core.chat.identity import resolve_speaker
    from coreman.core.db.models import Bot, BotMember, User

    speaker = await resolve_speaker(
        session, platform=run.platform, platform_user_id=platform_user_id
    )
    bot = await session.get(Bot, run.bot_id)
    user = await session.get(User, speaker.user_id) if speaker.user_id else None
    if bot is None or user is None:
        return False
    members = set(
        await session.scalars(select(BotMember.user_id).where(BotMember.bot_id == bot.id))
    )
    return is_bot_admin(user, bot, members)
