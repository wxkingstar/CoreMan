"""企业微信个人工具：成员本人的授权绑定、核对与调用。

企业微信的「可使用权限」绑在机器人上，机器人代表创建它的成员，调用里不带发言人。所以每位成员
自己扫码建一个只负责取数的「授权机器人」（扫码时点「确认授权」即授予全部能力），CoreMan 托管它的
凭证：这位成员在任意企业微信 AI 员工的私聊里，工具都用这份凭证、以本人身份执行。

绑定时向企业微信核对「授权人就是本人」，之后每隔几分钟再核一次：授权机器人被删、Secret 被重置
或换了授权人，这份绑定立刻作废，绝不拿别人的授权替本人干活。

能力授权按子项（例如「搜索与获取邮件」「发送邮件」）各自计时约 7 天，持续调用也不会顺延，企业微信
也没有查询到期时间的接口。这里按调用结果记录每项的状态（850002 未授权、850003 已过期、850001
链接失效），并按「最近一次授权时间 + 7 天」估算到期，用来提前提醒本人去续期。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import WecomPersonalBinding
from coreman.core.wecom_personal import gateway

LEVELS = ("readonly", "all_except_send", "all")
LEVEL_TITLES = {
    "readonly": "仅读取",
    "all_except_send": "读写（不含发邮件、共享文档）",
    "all": "全部（含发邮件、共享文档）",
}
# 复核授权人的间隔：每次调用都核会多一倍请求，太久又会让失效的绑定多干几轮活。
VERIFY_INTERVAL = timedelta(minutes=5)
# 企业微信能力授权的有效期：按子项各自计时，调用不顺延。
AUTH_TTL = timedelta(days=7)
# 页面与提醒按这个顺序列能力；键是企业微信网关的服务名。
SERVICES = {
    "contact": "通讯录",
    "todo": "待办",
    "calendar": "日程",
    "meeting": "会议",
    "doc": "文档与表格",
    "mail": "邮件",
    "disk": "微盘",
}
# 表格与文档共用「文档」这项授权。
SERVICE_ALIASES = {"sheet": "doc"}
STATE_BY_ERRCODE = {850001: "invalid", 850002: "unauthorized", 850003: "expired"}
# 企业微信的时间参数按企业所在时区解释。
_CN = ZoneInfo("Asia/Shanghai")
RETENTION_NOTICE = (
    "授权机器人代表你本人：只在你与 AI 员工的企业微信私聊、以及你本人创建且只发给你本人的"
    "定时任务里使用，群聊不会使用。使用期间的私聊与普通私聊一样保留在对话记录中，但只有你本人"
    "能查看，机器人管理员也看不到。暂停或解除绑定会停止后续使用，但不会删除已有记录。"
)


class PersonalError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def aad(row: WecomPersonalBinding, field: str) -> str:
    return f"wecom_personal_bindings.{field}:{row.user_id}"


def fingerprint(bot_id: str, secret: str) -> str:
    return hashlib.sha256(f"{bot_id}:{secret}".encode()).hexdigest()


def renew_hint(row: WecomPersonalBinding | None) -> str:
    name = (row.bot_name if row else None) or "你的授权机器人"
    return (
        f"请在电脑端企业微信「工作台 → 智能机器人 → {name} → 可使用权限」中授权或续期对应能力"
        "（手机端不支持在这里授权）。"
    )


async def _lock(session: AsyncSession, user_id: uuid.UUID) -> None:
    digest = hashlib.sha256(f"wecom-personal-binding:{user_id}".encode()).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def load(
    session: AsyncSession, user_id: uuid.UUID, *, create: bool = False
) -> WecomPersonalBinding | None:
    """加锁读取本人的绑定。绑定、复核、调用与解除都按这把锁串行。"""
    await _lock(session, user_id)
    if create:
        await session.execute(
            insert(WecomPersonalBinding).values(user_id=user_id).on_conflict_do_nothing()
        )
    row: WecomPersonalBinding | None = (
        await session.scalars(
            select(WecomPersonalBinding)
            .where(WecomPersonalBinding.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    return row


def usable(row: WecomPersonalBinding | None) -> bool:
    return row is not None and row.status == "bound" and row.enabled


def credentials(cipher: Cipher, row: WecomPersonalBinding) -> tuple[str, str]:
    if row.status != "bound" or not row.credentials_enc:
        raise PersonalError("authorization_required")
    data = json.loads(cipher.decrypt(row.credentials_enc, aad(row, "credentials_enc")))
    bot_id, secret = data.get("bot_id"), data.get("secret")
    if not isinstance(bot_id, str) or not isinstance(secret, str) or not bot_id or not secret:
        raise PersonalError("authorization_required")
    return bot_id, secret


def _rotate(row: WecomPersonalBinding) -> None:
    row.context_epoch = uuid.uuid4()


def unbind(row: WecomPersonalBinding, error: str | None = None) -> None:
    """删掉托管的凭证；企业微信里的授权机器人要本人自己删除。"""
    _rotate(row)
    row.status = "unbound"
    row.credentials_enc = row.token_enc = None
    row.wecom_bot_id = row.authorizer_id = row.authorizer_name = None
    row.bot_fingerprint = ""
    row.verified_at = row.bound_at = row.reminded_at = None
    row.capabilities = {}
    row.error = error


def bind(
    row: WecomPersonalBinding,
    cipher: Cipher,
    *,
    bot_id: str,
    secret: str,
    token: str,
    identity: gateway.Identity,
) -> None:
    """换成一份新核对过的凭证：旧凭证、旧令牌与签出去的能力凭据一并作废。"""
    now = _now()
    _rotate(row)
    row.status = "bound"
    row.enabled = True
    row.wecom_bot_id = bot_id
    row.credentials_enc = cipher.encrypt(
        json.dumps({"bot_id": bot_id, "secret": secret}), aad(row, "credentials_enc")
    )
    row.bot_fingerprint = fingerprint(bot_id, secret)
    row.token_enc = cipher.encrypt(token, aad(row, "token_enc"))
    row.authorizer_id = identity.authorizer_id
    row.authorizer_name = identity.authorizer_name
    row.bot_name = identity.bot_name
    row.verified_at = row.bound_at = now
    row.reminded_at = None
    row.capabilities = {}
    row.error = None


def set_level(row: WecomPersonalBinding, level: str) -> None:
    if level not in LEVELS:
        raise PersonalError("invalid_level")
    if row.status != "bound":
        raise PersonalError("authorization_required")
    _rotate(row)
    row.authorization_level = level
    row.enabled = True


def set_enabled(row: WecomPersonalBinding, enabled: bool) -> None:
    if row.status != "bound":
        raise PersonalError("authorization_required")
    if row.enabled != enabled:
        _rotate(row)
    row.enabled = enabled


async def _with_token[T](
    cipher: Cipher, row: WecomPersonalBinding, call: Callable[[str], Awaitable[T]]
) -> T:
    """用保存的令牌调用；令牌过期或无效就用凭证重新签名换一个，再试一次。"""
    bot_id, secret = credentials(cipher, row)
    token = cipher.decrypt(row.token_enc, aad(row, "token_enc")) if row.token_enc else None
    for attempt in range(2):
        if token is None:
            token = await gateway.fetch_token(bot_id, secret)
            row.token_enc = cipher.encrypt(token, aad(row, "token_enc"))
        try:
            return await call(token)
        except gateway.GatewayError as exc:
            if exc.code != "token_expired" or attempt:
                raise
            token = None
    raise AssertionError("unreachable")


async def verify(cipher: Cipher, row: WecomPersonalBinding) -> None:
    """确认绑定仍然有效：凭证没被拒、授权机器人代表的仍是本人。"""
    bot_id, secret = credentials(cipher, row)
    now = _now()
    current = fingerprint(bot_id, secret)
    if (
        row.bot_fingerprint == current
        and row.verified_at is not None
        and now - row.verified_at < VERIFY_INTERVAL
    ):
        return
    if row.bot_fingerprint != current:
        # 凭证换过：旧令牌签给的是旧 Secret，重新换取并核对授权人。
        row.token_enc = None
        row.bot_fingerprint = current
    try:
        identity = await _with_token(cipher, row, gateway.whoami)
    except gateway.GatewayError as exc:
        if exc.code == "credentials_rejected":
            # 授权机器人被删除或重置了 Secret：凭证再也用不了，只能重新扫码绑定。
            unbind(row, "credentials_rejected")
            raise PersonalError("credentials_rejected") from exc
        raise PersonalError("upstream_unavailable") from exc
    if identity.bot_id != bot_id or identity.authorizer_id != row.authorizer_id:
        unbind(row, "authorizer_changed")
        raise PersonalError("authorization_changed")
    row.verified_at = now


async def call[T](
    cipher: Cipher, row: WecomPersonalBinding, invoke: Callable[[str], Awaitable[T]]
) -> T:
    """复核之后以本人身份调用；企业微信的业务错误原样抛给调用方。"""
    await verify(cipher, row)
    try:
        return await _with_token(cipher, row, invoke)
    except gateway.GatewayError as exc:
        if exc.code == "wecom_error":
            raise
        if exc.code == "credentials_rejected":
            unbind(row, "credentials_rejected")
            raise PersonalError("credentials_rejected") from exc
        raise PersonalError("upstream_unavailable") from exc


# ---------------------------------------------------------------- 能力状态


def capability_key(service: str, kind: str) -> str:
    return f"{SERVICE_ALIASES.get(service, service)}:{kind}"


def record(
    row: WecomPersonalBinding,
    key: str,
    *,
    errcode: int | None = None,
    help_url: str | None = None,
) -> None:
    """按一次调用的结果更新这项能力的状态；errcode 为空表示成功。"""
    now = _now()
    caps = dict(row.capabilities or {})
    raw = caps.get(key)
    previous: dict[str, Any] = raw if isinstance(raw, dict) else {}
    entry: dict[str, Any] = {**previous, "checked_at": _iso(now)}
    if errcode is None:
        if previous.get("state") != "ok" or not previous.get("authorized_at"):
            # 第一次见到成功：扫码时点「确认授权」会一次授予全部能力，按绑定时间起算；
            # 从失败变成功，说明本人刚在企业微信里授权或续期过。
            fresh = not previous and row.bound_at is not None and now - row.bound_at < AUTH_TTL
            entry["authorized_at"] = _iso(row.bound_at if fresh and row.bound_at else now)
        entry["state"] = "ok"
        entry.pop("renew_url", None)
    else:
        entry["state"] = STATE_BY_ERRCODE.get(errcode, "error")
        entry["errcode"] = errcode
        if help_url:
            entry["renew_url"] = help_url
    caps[key] = entry
    row.capabilities = caps


def record_error(row: WecomPersonalBinding, key: str, exc: gateway.GatewayError) -> None:
    """只记录企业微信明确的能力授权错误；参数错误、网络问题不改变能力状态。"""
    if exc.code == "wecom_error" and exc.errcode in STATE_BY_ERRCODE:
        record(row, key, errcode=exc.errcode, help_url=exc.help_url)


def expires_at(entry: dict[str, Any]) -> datetime | None:
    if entry.get("state") != "ok":
        return None
    start = _parse(entry.get("authorized_at"))
    return start + AUTH_TTL if start else None


def next_expiry(row: WecomPersonalBinding) -> datetime | None:
    moments = [
        moment
        for entry in (row.capabilities or {}).values()
        if isinstance(entry, dict) and (moment := expires_at(entry)) is not None
    ]
    return min(moments) if moments else None


def mark_renewed(row: WecomPersonalBinding) -> None:
    """本人说已在企业微信里续期：当前能用的各项从现在重新起算。"""
    now = _iso(_now())
    caps = dict(row.capabilities or {})
    for key, entry in caps.items():
        if isinstance(entry, dict) and entry.get("state") == "ok":
            caps[key] = {**entry, "authorized_at": now}
    row.capabilities = caps
    row.reminded_at = None


def _window() -> tuple[str, str]:
    today = _now().astimezone(_CN).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return today.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _recent() -> tuple[str, str]:
    now = _now().astimezone(_CN)
    return (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"), now.strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def probe_calls(row: WecomPersonalBinding) -> dict[str, tuple[str, dict[str, Any]]]:
    """每项能力一次只读调用，参数取实测可用的最小集合。"""
    begin, end = _window()
    since, until = _recent()
    calls: dict[str, tuple[str, dict[str, Any]]] = {
        "todo": ("/todo/list", {"limit": 1}),
        "calendar": ("/calendar/schedules/list", {"begin_time": begin, "end_time": end}),
        "meeting": ("/meeting/list", {"begin_time": begin, "end_time": end, "limit": 1}),
        "doc": ("/doc/search", {"keywords": ["周报"], "limit": 1}),
        "mail": ("/mail/search", {"begin_time": since, "end_time": until}),
        "disk": ("/disk/files/list", {"limit": 1}),
    }
    if row.authorizer_name:
        calls["contact"] = ("/contact/users/search", {"keywords": [row.authorizer_name]})
    return calls


async def probe(cipher: Cipher, row: WecomPersonalBinding) -> None:
    """逐项试一次只读调用并记录状态。企业微信暂时不可用时保留原状态。"""
    await verify(cipher, row)

    async def one(service: str, path: str, payload: dict[str, Any]) -> None:
        try:
            await _with_token(cipher, row, lambda token: gateway.invoke(token, path, payload))
        except gateway.GatewayError as exc:
            record_error(row, capability_key(service, "read"), exc)
            return
        record(row, capability_key(service, "read"))

    await asyncio.gather(
        *(one(service, path, payload) for service, (path, payload) in probe_calls(row).items())
    )


def summary(row: WecomPersonalBinding) -> dict[str, list[str]]:
    """哪些能力现在用不了：给提示词与提醒用的可读名单。"""
    out: dict[str, list[str]] = {"unauthorized": [], "expired": [], "invalid": []}
    for key, entry in (row.capabilities or {}).items():
        if not isinstance(entry, dict):
            continue
        state = entry.get("state")
        if state in out:
            service, _, kind = key.partition(":")
            suffix = {"write": "（写入）", "send": "（发送）"}.get(kind, "")
            label = SERVICES.get(service, service) + suffix
            if label not in out[state]:
                out[state].append(label)
    return out


def state(row: WecomPersonalBinding | None) -> dict[str, Any]:
    """「我的企业微信」页面看到的全部状态；加密字段一律不下发。"""
    if row is None:
        return {
            "status": "unbound",
            "enabled": False,
            "authorization_level": "readonly",
            "wecom_bot_id": None,
            "bot_name": None,
            "authorizer_name": None,
            "bound_at": None,
            "verified_at": None,
            "next_expiry": None,
            "error": None,
            "capabilities": [],
            "retention_notice": RETENTION_NOTICE,
        }
    caps = row.capabilities or {}
    services = []
    for service in SERVICES:
        kinds: dict[str, Any] = {}
        for key, entry in caps.items():
            name, _, kind = key.partition(":")
            if name != service or not isinstance(entry, dict):
                continue
            moment = expires_at(entry)
            kinds[kind] = {
                "state": entry.get("state", "unknown"),
                "checked_at": entry.get("checked_at"),
                "expires_at": _iso(moment) if moment else None,
                "renew_url": entry.get("renew_url"),
            }
        services.append(
            {"service": service, **{k: kinds.get(k) for k in ("read", "write", "send")}}
        )
    moment = next_expiry(row) if row.status == "bound" else None
    return {
        "status": row.status,
        "enabled": bool(row.enabled) and row.status == "bound",
        "authorization_level": row.authorization_level,
        "wecom_bot_id": row.wecom_bot_id,
        "bot_name": row.bot_name,
        "authorizer_name": row.authorizer_name,
        "bound_at": _iso(row.bound_at) if row.bound_at else None,
        "verified_at": _iso(row.verified_at) if row.verified_at else None,
        "next_expiry": _iso(moment) if moment else None,
        "error": row.error,
        "capabilities": services if row.status == "bound" else [],
        "retention_notice": RETENTION_NOTICE,
    }
