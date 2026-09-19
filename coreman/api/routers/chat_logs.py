"""对话记录查询与统计（chat_logs）。

任何登录角色都能看，但只能看见自己有份的那部分：`accessible_bot_ids`
给出可见的 bot 集合，再并上「我自己说过的话」。列表只回摘要，全文要单独取详情——
聊天内容是最敏感的数据，能不广播就不广播（日志里同样一个字都不写）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import ColumnElement, and_, case, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.bot_names import bot_names
from coreman.api.deps import current_user, get_session, via_bot_token
from coreman.api.errors import not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.routers.audit_logs import escape_like
from coreman.api.security import verify_csrf
from coreman.core.db.models import Bot, BotMember, ChatLog, CronRun, User
from coreman.core.timeutils import aware_utc

router = APIRouter(
    prefix="/api/admin/chat-logs", tags=["chat-logs"], dependencies=[Depends(verify_csrf)]
)
PREVIEW_CHARS = 200
TOP_BOTS = 50
# 「出错了」这一列把三种失败态算在一起：只数 status='error' 会让全是超时的 bot 看着很健康。
FAILURE_STATUSES = ("error", "timeout", "failed")


async def accessible_bot_ids(session: AsyncSession, user: User) -> set[uuid.UUID] | None:
    """可见的 bot 集合；`None` 表示不限（ai_committee / platform_admin 看全量）。"""
    if user.role in ("ai_committee", "platform_admin"):
        return None
    owned = select(Bot.id).where(Bot.created_by == user.id)
    member = select(BotMember.bot_id).where(BotMember.user_id == user.id)
    ids = set((await session.execute(owned)).scalars()) | set(
        (await session.execute(member)).scalars()
    )
    if user.role == "team_lead" and user.team_id is not None:
        team_bots = select(Bot.id).where(Bot.team_id == user.team_id)
        ids |= set((await session.execute(team_bots)).scalars())
    return ids


def _scope(ids: set[uuid.UUID] | None, user: User, *, bot_token: bool) -> list[ColumnElement[bool]]:
    """可见性条件：我管得着的 bot，或者我自己参与过的对话。"""
    # 飞书私聊、挂了本人企业微信工具的轮次，以及以本人身份运行的定时任务：只有本人能看。
    private = or_(
        and_(ChatLog.platform == "feishu", ChatLog.chat_type == "single"),
        ChatLog.private.is_(True),
        and_(
            ChatLog.chat_type == "cron",
            ChatLog.task_id.in_(select(CronRun.task_id).where(CronRun.private.is_(True))),
        ),
    )
    # A bot token can originate in a group, even when its human identity is the owner.
    privacy = not_(private) if bot_token else or_(not_(private), ChatLog.user_id == user.id)
    if ids is None:
        return [privacy]
    return [privacy, or_(ChatLog.bot_id.in_(ids), ChatLog.user_id == user.id)]


def _window(
    bot_id: uuid.UUID | None, since: datetime | None, until: datetime | None
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    if bot_id is not None:
        conds.append(ChatLog.bot_id == bot_id)
    if since is not None:
        conds.append(ChatLog.request_at >= aware_utc(since))
    if until is not None:
        conds.append(ChatLog.request_at <= aware_utc(until))
    return conds


def _filters(
    user: str | None, status: str | None, chat_type: str | None, keyword: str | None
) -> list[ColumnElement[bool]]:
    """Keep list and aggregate queries on the same visible filter set."""
    conds: list[ColumnElement[bool]] = []
    if user:
        like = ChatLog.user_login.ilike(f"%{escape_like(user)}%", escape="\\")
        try:
            user_id = uuid.UUID(user)
        except ValueError:
            conds.append(like)
        else:
            # 合法 uuid 既可能是 user_id，也可能有人把 uuid 当登录名搜，两边都试。
            conds.append(or_(ChatLog.user_id == user_id, like))
    if status:
        conds.append(ChatLog.status == status)
    if chat_type:
        conds.append(ChatLog.chat_type == chat_type)
    if keyword:
        pattern = f"%{escape_like(keyword)}%"
        conds.append(
            or_(
                ChatLog.message_content.ilike(pattern, escape="\\"),
                ChatLog.response_content.ilike(pattern, escape="\\"),
            )
        )
    return conds


def _preview(value: str | None) -> str | None:
    return None if value is None else value[:PREVIEW_CHARS]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def chat_log_out(
    row: ChatLog, *, full: bool = False, bot_name: str | None = None
) -> dict[str, Any]:
    """列表项只有摘要；`full=True` 才带正文、报错详情与成本。"""
    data: dict[str, Any] = {
        "id": row.id,
        "bot_id": str(row.bot_id),
        "bot_key": row.bot_key,
        "bot_name": bot_name,
        "platform": row.platform,
        "user_id": str(row.user_id) if row.user_id else None,
        "user_login": row.user_login,
        "user_name": row.user_name,
        "chat_type": row.chat_type,
        "chat_id": row.chat_id,
        "session_key": row.session_key,
        "relay_session_id": str(row.relay_session_id) if row.relay_session_id else None,
        "model": row.model,
        "stream_id": row.stream_id,
        "task_id": row.task_id,
        "message_type": row.message_type,
        "message_preview": _preview(row.message_content),
        "response_preview": _preview(row.response_content),
        "tools_used": list(row.tools_used or []),
        "status": row.status,
        "error_code": row.error_code,
        "latency_ms": row.latency_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cache_read_tokens": row.cache_read_tokens,
        "cache_creation_tokens": row.cache_creation_tokens,
        "request_at": _iso(row.request_at),
        "response_at": _iso(row.response_at),
    }
    if full:
        data |= {
            "message_content": row.message_content,
            "quoted_content": row.quoted_content,
            "file_info": row.file_info,
            "response_content": row.response_content,
            "error_message": row.error_message,
            "relay_server_id": str(row.relay_server_id) if row.relay_server_id else None,
            # Numeric 列读出来是 Decimal，直接进 JSON 会 500。
            "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
        }
    return data


@router.get("")
async def list_chat_logs(
    request: Request,
    bot_id: uuid.UUID | None = None,
    user: str | None = None,
    status: str | None = None,
    chat_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    keyword: str | None = Query(default=None, max_length=200),
    actor: User = Depends(current_user),
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    ids = await accessible_bot_ids(session, actor)
    conds = _scope(ids, actor, bot_token=via_bot_token(request)) + _window(bot_id, since, until)
    conds += _filters(user, status, chat_type, keyword)
    # request_at 会并列（同一秒的批量写），加 id 兜底保证翻页稳定。
    stmt = select(ChatLog).where(*conds).order_by(ChatLog.request_at.desc(), ChatLog.id.desc())
    page = await paginate(session, stmt, params)
    rows: list[ChatLog] = page["items"]
    names = await bot_names(session, (r.bot_id for r in rows))
    return {
        "code": 0,
        "data": {**page, "items": [chat_log_out(r, bot_name=names.get(r.bot_id)) for r in rows]},
    }


@router.get("/stats")
async def chat_log_stats(
    request: Request,
    bot_id: uuid.UUID | None = None,
    user: str | None = None,
    status: str | None = None,
    chat_type: str | None = None,
    keyword: str | None = Query(default=None, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """概览：总量、按状态、平均时延、token 合计、按 bot 前 50（同样受可见性过滤）。"""
    ids = await accessible_bot_ids(session, actor)
    conds = _scope(ids, actor, bot_token=via_bot_token(request)) + _window(bot_id, since, until)
    conds += _filters(user, status, chat_type, keyword)

    def _tokens(col: Any) -> Any:
        return func.coalesce(func.sum(col), 0)

    totals = (
        await session.execute(
            select(
                func.count(),
                func.avg(ChatLog.latency_ms),
                _tokens(ChatLog.input_tokens),
                _tokens(ChatLog.output_tokens),
                _tokens(ChatLog.cache_read_tokens),
                _tokens(ChatLog.cache_creation_tokens),
            ).where(*conds)
        )
    ).one()
    by_status = (
        await session.execute(
            select(ChatLog.status, func.count()).where(*conds).group_by(ChatLog.status)
        )
    ).all()
    success = func.sum(case((ChatLog.status == "success", 1), else_=0))
    failed = func.sum(case((ChatLog.status.in_(FAILURE_STATUSES), 1), else_=0))
    by_bot = (
        await session.execute(
            select(
                ChatLog.bot_id,
                ChatLog.bot_key,
                func.count().label("total"),
                success,
                failed,
                func.avg(ChatLog.latency_ms),
            )
            .where(*conds)
            .group_by(ChatLog.bot_id, ChatLog.bot_key)
            .order_by(func.count().desc(), ChatLog.bot_key)
            .limit(TOP_BOTS)
        )
    ).all()
    names = await bot_names(session, (r[0] for r in by_bot))
    return {
        "code": 0,
        "data": {
            "total": int(totals[0]),
            "by_status": {str(s): int(n) for s, n in by_status},
            "avg_latency_ms": _avg_ms(totals[1]),
            "tokens": {
                "input": int(totals[2]),
                "output": int(totals[3]),
                "cache_read": int(totals[4]),
                "cache_creation": int(totals[5]),
            },
            "by_bot": [
                {
                    "bot_id": str(row[0]),
                    "bot_key": row[1],
                    "bot_name": names.get(row[0]),
                    "total": int(row[2]),
                    "success": int(row[3] or 0),
                    "error": int(row[4] or 0),
                    "avg_latency_ms": _avg_ms(row[5]),
                }
                for row in by_bot
            ],
        },
    }


def _avg_ms(value: Any) -> int | None:
    """avg() 读出来是 Decimal（且没有数据时是 None）：统一成整毫秒。"""
    return None if value is None else int(round(float(value)))


@router.get("/{log_id}")
async def get_chat_log(
    request: Request,
    log_id: int,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    ids = await accessible_bot_ids(session, actor)
    stmt = select(ChatLog).where(
        ChatLog.id == log_id, *_scope(ids, actor, bot_token=via_bot_token(request))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        # 看不见的记录一律说「不存在」，不泄漏「有这条但你没权限」。
        raise not_found("对话记录不存在")
    names = await bot_names(session, [row.bot_id])
    return {"code": 0, "data": chat_log_out(row, full=True, bot_name=names.get(row.bot_id))}
