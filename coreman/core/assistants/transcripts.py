"""个人助理的 CLI 转录：平台保管，节点每轮取用后清除。

一轮的交接：
1. 派出之前（`prepare`，worker 开流时，持有机器人行锁）：定下这一轮续不续上一轮的转录，写下
   本轮的交接编号 `pending_transfer` 与所派节点。请求体带 `transcript: {"transfer", "restore"}`；
2. 节点要续聊就凭编号来取（`download`），一轮结束凭同一编号上传（`upload`），平台清掉编号；
3. 下一轮派出时编号还在，说明上一轮的转录没传回来（节点挂了、网断了）：不拿旧的那份续，
   换一个新的 relay 会话。
4. 例外是上一轮被取消：节点收到取消后先停沙箱、再在后台上传，下一轮往往先到。开轮之前
   （`awaiting_upload`，不占事务）最多等 `UPLOAD_WAIT`，等到了照常续，等不到再按第 3 条办。

内容是节点打好的 tar.gz，经 ORM 写入时由 `sealed` 用所有者的数据密钥加密（`blob_enc` 是二进制
内容列）。日志只记代码、编号与大小。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from coreman.core.assistants import sealed
from coreman.core.chat import sessions
from coreman.core.db.models import AssistantTranscript, Bot, ChatLog, ChatSession, Task

MAX_BYTES = 50 * 1024 * 1024
# purge 一条语句最多删这么多行（转录行可达 50 MiB，不让调度器的一条语句删出一大片）。
PURGE_BATCH = 200
# 一次 purge 最多处理这么多批（每个调度周期最多约 2000 行，其余下个周期再删）。
PURGE_MAX_BATCHES = 10
# 节点上传时说明这一轮的转录怎么了：ok 带内容；empty 没有转录；too_large 超过上限；
# unchanged 节点没跑这一轮（拒绝了），上一份照旧可用。
STATUSES = ("ok", "empty", "too_large", "unchanged")
# 被取消的一轮：节点停沙箱（至多 10 秒）之后才在后台上传转录。下一轮开轮前最多等它这么久，从那
# 一轮结束的时刻起算；过了还没到就不等了。
UPLOAD_WAIT = timedelta(seconds=20)


@dataclass(frozen=True)
class Plan:
    transfer: uuid.UUID
    restore: bool

    def body(self) -> dict[str, Any]:
        """请求体里的 `transcript` 字段。"""
        return {"transfer": self.transfer.hex, "restore": self.restore}


def _key(bot_id: uuid.UUID, session_id: uuid.UUID) -> Any:
    return (AssistantTranscript.bot_id == bot_id) & (AssistantTranscript.session_id == session_id)


async def prepare(
    session: AsyncSession,
    *,
    bot: Bot,
    node_id: uuid.UUID,
    info: sessions.SessionInfo,
    session_key: str,
    backend: str,
    speaker_user_id: uuid.UUID | None,
) -> tuple[sessions.SessionInfo, Plan]:
    """派出一轮之前：返回本轮实际用的会话（可能换了新的）与交接计划。调用方负责提交。

    续得上的条件：不是新会话、库里有上一轮的转录、那一轮的交接已办完、数据密钥可用。
    续不上又不是新会话时换一个新的 relay 会话：驱动拿着旧会话号续，会在节点上悄悄从空会话开始，
    换新的至少让这一轮明明白白是新会话。例外是换运行时后的首轮（会话带着 `inherited_from`，
    且没有转录行，或者只有一行交接已了结、没有内容的——首轮排队超时被 `release` 作废编号后就是
    这样）：节点上没有可续的旧状态，原样用这个会话号（不还原），平台的上下文说明才带得进这一轮。
    """
    restore = False
    if not info.is_new:
        row = (
            await session.execute(
                select(AssistantTranscript.pending_transfer, AssistantTranscript.size)
                .where(_key(bot.id, info.relay_session_id))
                .with_for_update()
            )
        ).first()
        restore = bool(
            row is not None
            and row[0] is None
            and row[1] > 0
            and await sealed.key_ready(session, bot.id)
        )
        carried = info.inherited_from is not None and (
            row is None or (row[0] is None and row[1] == 0)
        )
        if not restore and not carried:
            await session.execute(
                delete(AssistantTranscript).where(_key(bot.id, info.relay_session_id))
            )
            info = await sessions.renew(
                session,
                bot_id=bot.id,
                session_key=session_key,
                backend=backend,
                speaker_user_id=speaker_user_id,
            )
    transfer = uuid.uuid4()
    stmt = insert(AssistantTranscript).values(
        bot_id=bot.id,
        session_id=info.relay_session_id,
        pending_transfer=transfer,
        pending_node_id=node_id,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[AssistantTranscript.bot_id, AssistantTranscript.session_id],
            set_={
                "pending_transfer": transfer,
                "pending_node_id": node_id,
                "updated_at": datetime.now(UTC),
            },
        )
    )
    return info, Plan(transfer, restore)


async def awaiting_upload(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    session_key: str,
    task_id: int,
    now: datetime | None = None,
) -> datetime | None:
    """这个会话上一轮的转录是不是还在路上：返回最晚等到什么时候，不用等返回 None。纯读。

    要等的只有一种情况：会话当前的交接编号还没了结，而留下它的那一轮是被取消的（所有者的新
    消息打断了它，或他喊了停）。那一轮到过节点——确定没到过的在收尾时已由 `release` 清掉
    编号——节点正在后台停沙箱、上传。正常结束的轮次在收尾之前就传完了，编号还在说明确实没传
    上来；worker 失联的轮次传不传、什么时候传都说不准。这两种不等，照旧换新会话。

    留下编号的那一轮按对话记录认：开流时这一轮的记录与交接编号在同一事务里写下，带着会话号
    与任务号。`task_id` 是正要开的这一轮，不算在内。
    """
    moment = now or datetime.now(UTC)
    relay_session_id = await session.scalar(
        select(ChatSession.relay_session_id).where(
            ChatSession.bot_id == bot_id, ChatSession.session_key == session_key
        )
    )
    if relay_session_id is None:
        return None
    pending = await session.scalar(
        select(AssistantTranscript.pending_transfer).where(_key(bot_id, relay_session_id))
    )
    if pending is None:
        return None
    last = (
        await session.execute(
            select(Task.status, Task.finished_at)
            .join(ChatLog, ChatLog.task_id == Task.id)
            .where(
                ChatLog.bot_id == bot_id,
                ChatLog.session_key == session_key,
                ChatLog.relay_session_id == relay_session_id,
                Task.id != task_id,
            )
            .order_by(ChatLog.id.desc())
            .limit(1)
        )
    ).first()
    if last is None or last[0] != "cancelled" or last[1] is None:
        return None
    until: datetime = last[1] + UPLOAD_WAIT
    return until if until > moment else None


async def release(
    session: AsyncSession, *, bot_id: uuid.UUID, session_id: uuid.UUID, transfer: uuid.UUID
) -> bool:
    """这一轮确定没到过节点（排队时就撤销了）：作废它的交接编号，上一份转录下一轮照旧可续。

    只在编号还是这一轮的时候清（之后的轮次已换上新编号就不动）；返回是否清了。调用方负责提交。
    有一点不确定节点是否见过请求的，不要调用：留着编号，下一轮开新会话（不会续上可能过期的转录）。
    """
    result = await session.execute(
        update(AssistantTranscript)
        .where(_key(bot_id, session_id), AssistantTranscript.pending_transfer == transfer)
        .values(pending_transfer=None, pending_node_id=None)
    )
    return bool(getattr(result, "rowcount", 0))


async def download(
    session: AsyncSession, *, node_id: uuid.UUID, transfer: uuid.UUID
) -> bytes | None:
    """节点续聊前来取。编号或节点对不上、内容读不出来（密钥已销毁、解不开）都返回 None。

    调用方要先对会话调 `sealed.tolerate_undecryptable`：解不开时按没有处理，节点从新会话开始。
    """
    key = (
        await session.execute(
            select(AssistantTranscript.bot_id, AssistantTranscript.session_id).where(
                AssistantTranscript.pending_transfer == transfer,
                AssistantTranscript.pending_node_id == node_id,
            )
        )
    ).first()
    if key is None:
        return None
    row = await session.get(AssistantTranscript, (key[0], key[1]), populate_existing=True)
    if row is None or sealed.is_locked(row) or not row.blob_enc:
        return None
    return bytes(row.blob_enc)


async def upload(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    transfer: uuid.UUID,
    status: str,
    data: bytes,
) -> str:
    """节点交回本轮的转录，返回 stored / kept / cleared，对不上号返回 stale。调用方负责提交。"""
    row = await session.scalar(
        select(AssistantTranscript)
        .where(
            AssistantTranscript.pending_transfer == transfer,
            AssistantTranscript.pending_node_id == node_id,
        )
        .options(defer(AssistantTranscript.blob_enc))
        .with_for_update()
    )
    if row is None:
        return "stale"
    row.pending_transfer = None
    row.pending_node_id = None
    row.updated_at = datetime.now(UTC)
    if status == "unchanged":
        result = "kept"
    elif status == "ok" and data:
        row.blob_enc, row.size, result = data, len(data), "stored"
    else:
        row.blob_enc, row.size, result = None, 0, "cleared"
    await session.flush()
    return result


async def purge(session: AsyncSession, before: datetime) -> int:
    """删掉在 `before` 之前就不再更新的转录（会话早已过期，不会再续）。调用方负责提交。

    每批只选没被别的事务锁住的行（上传正持有的行跳过、不等），一次最多处理 `PURGE_MAX_BATCHES`
    批，剩下的留给之后的调用。
    """
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        keys = (
            await session.execute(
                select(AssistantTranscript.bot_id, AssistantTranscript.session_id)
                .where(AssistantTranscript.updated_at < before)
                .limit(PURGE_BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not keys:
            break
        await session.execute(
            delete(AssistantTranscript).where(
                tuple_(AssistantTranscript.bot_id, AssistantTranscript.session_id).in_(
                    [(row[0], row[1]) for row in keys]
                )
            )
        )
        total += len(keys)
        if len(keys) < PURGE_BATCH:
            break
    return total
