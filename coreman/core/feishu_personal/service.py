"""Server-owned Feishu OAuth credentials and read-only, identity-bound API calls."""

from __future__ import annotations

import hashlib
import json as jsonlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import FeishuPersonalGrant
from coreman.core.feishu_personal import permissions, revocation
from coreman.core.feishu_personal.policy import Scope, app_credentials

BASE = "https://open.feishu.cn/open-apis"
TOKEN_URL = BASE + "/authen/v2/oauth/token"
SCOPES = (
    "offline_access auth:user.id:read search:message im:message:readonly "
    "im:message.group_msg:get_as_user im:message.p2p_msg:get_as_user im:chat:read "
    "vc:meeting.search:read vc:meeting:readonly vc:note:read "
    "minutes:minutes.search:read minutes:minutes.basic:read minutes:minutes.artifacts:read "
    "docx:document:readonly"
)


class PersonalError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _aad(row: FeishuPersonalGrant, field: str) -> str:
    return f"feishu_personal_grants.{field}:{row.bot_id}:{row.user_id}:{row.app_id}"


def _fingerprint(app_id: str, secret: str) -> str:
    return hashlib.sha256((app_id + ":" + secret).encode()).hexdigest()


def _clear(row: FeishuPersonalGrant, status: str = "revoked") -> None:
    row.context_epoch = uuid.uuid4()
    row.status = status
    row.token_enc = row.pending_enc = None
    row.expires_at = row.pending_expires_at = row.next_poll_at = None
    row.scopes = []
    row.requested_scopes = []
    row.selection_chat_id = None


def save_tokens(
    cipher: Cipher, row: FeishuPersonalGrant, data: dict[str, Any], secret: str
) -> None:
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise PersonalError("authorization_failed")
    # The explicit selection/link issuance already started a new generation.
    # Completing verified OAuth (or refreshing) must keep this attempt usable.
    row.token_enc = cipher.encrypt(
        jsonlib.dumps({k: data[k] for k in ("access_token", "refresh_token") if data.get(k)}),
        _aad(row, "token_enc"),
    )
    row.expires_at = datetime.now(UTC) + timedelta(
        seconds=max(1, int(data.get("expires_in", 7200)))
    )
    if data.get("scope"):
        row.scopes = str(data["scope"]).split()
    row.app_fingerprint = _fingerprint(row.app_id, secret)
    row.status = "connected"
    row.remote_revoked = False
    row.pending_enc = None
    row.pending_expires_at = row.next_poll_at = None


async def _http(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            # Bound upstream payload size as minutes may contain very long transcripts.
            async with client.stream(method, url, **kwargs) as response:
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 8_000_000:
                        raise PersonalError("response_too_large")
                data = jsonlib.loads(chunks)
                if not isinstance(data, dict):
                    raise PersonalError("upstream_unavailable")
                if response.status_code >= 400 and not data.get("error"):
                    raise PersonalError("upstream_unavailable")
                return data
    except (httpx.HTTPError, ValueError) as exc:
        raise PersonalError("upstream_unavailable") from exc


async def _lock(session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID) -> None:
    digest = hashlib.sha256(f"feishu-personal:{bot_id}:{user_id}".encode()).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _credentials(cipher: Cipher, scope: Scope) -> tuple[str, str]:
    try:
        app_id, secret = app_credentials(cipher, scope.bot)
    except ValueError as exc:
        raise PersonalError("feishu_app_unavailable") from exc
    if app_id != scope.app_id:
        raise PersonalError("app_identity_mismatch")
    return app_id, secret


async def _locked(
    session: AsyncSession, scope: Scope, app_id: str, secret: str
) -> FeishuPersonalGrant | None:
    row = (
        await session.scalars(
            select(FeishuPersonalGrant)
            .where(
                FeishuPersonalGrant.bot_id == scope.bot.id,
                FeishuPersonalGrant.user_id == scope.user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    expected = (
        app_id,
        str(scope.platform_user_id),
        str(scope.open_id),
        scope.tenant_key,
        _fingerprint(app_id, secret),
    )
    actual = (row.app_id, row.platform_user_id, row.open_id, row.tenant_key, row.app_fingerprint)
    if actual != expected:
        _clear(row)
        row.app_id, row.platform_user_id, row.open_id, row.tenant_key, row.app_fingerprint = (
            expected
        )
    return row


async def _row(
    session: AsyncSession, cipher: Cipher, scope: Scope
) -> tuple[FeishuPersonalGrant, str]:
    await _lock(session, scope.bot.id, scope.user_id)
    app_id, secret = _credentials(cipher, scope)
    # Upsert plus row lock serializes authorize, refresh, revoke and reads for this owner.
    await session.execute(
        insert(FeishuPersonalGrant)
        .values(
            bot_id=scope.bot.id,
            user_id=scope.user_id,
            app_id=app_id,
            app_fingerprint=_fingerprint(app_id, secret),
            platform_user_id=scope.platform_user_id,
            open_id=scope.open_id,
            tenant_key=scope.tenant_key,
            status="revoked",
            scopes=[],
            poll_interval=5,
        )
        .on_conflict_do_nothing()
    )
    row = await _locked(session, scope, app_id, secret)
    assert row is not None
    return row, secret


async def existing_row(
    session: AsyncSession, cipher: Cipher, scope: Scope
) -> FeishuPersonalGrant | None:
    """Same lock and identity checks as `_row`, without creating a grant for a new user."""
    await _lock(session, scope.bot.id, scope.user_id)
    app_id, secret = _credentials(cipher, scope)
    return await _locked(session, scope, app_id, secret)


RETENTION_NOTICE = (
    "授权只在你和机器人的私聊里生效，群聊不会使用；"
    "你本人创建、结果只发给你本人的定时任务也可以使用。"
    "私聊内容和回答与普通私聊一样保留在对话记录中，只有你本人能查看；"
    "撤销授权会停止后续读取，但不会删除已有记录。"
)


def _state(row: FeishuPersonalGrant, cipher: Cipher) -> dict[str, Any]:
    now = datetime.now(UTC)
    refresh_available = False
    if row.status == "connected" and row.token_enc:
        tokens = jsonlib.loads(cipher.decrypt(row.token_enc, _aad(row, "token_enc")))
        refresh_available = bool(tokens.get("refresh_token"))
    return {
        "retention_notice": RETENTION_NOTICE,
        "access_token_expired": bool(row.expires_at and row.expires_at <= now)
        if row.status == "connected"
        else None,
        "refresh_available": refresh_available,
        "checked_at_beijing": now.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        "status": row.status,
        "scopes": row.scopes,
        "authorization_level": row.authorization_level,
        "requested_scopes": row.requested_scopes,
        "missing_scopes": sorted(set(row.requested_scopes or []) - set(row.scopes or [])),
        "checked_at": now.isoformat(),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


SELECTION_PROMPT = (
    "请选择卡片中的授权范围，点击后生成授权链接。个人授权仅限本人与机器人的私聊使用。"
    + "\n"
    + RETENTION_NOTICE
)


async def _revoke_remote(cipher: Cipher, row: FeishuPersonalGrant, secret: str) -> bool:
    if not row.token_enc:
        return True
    try:
        tokens = jsonlib.loads(cipher.decrypt(row.token_enc, _aad(row, "token_enc")))
        return await revocation.revoke_tokens(row.app_id, secret, tokens)
    except (ValueError, TypeError, KeyError):
        return False


async def begin_selection(session: AsyncSession, cipher: Cipher, scope: Scope) -> dict[str, Any]:
    row, secret = await _row(session, cipher, scope)
    remote_revoked = await _revoke_remote(cipher, row, secret) if row.token_enc else True
    row.remote_revoked = remote_revoked
    _clear(row, "selecting")
    row.authorization_level = "messages_readonly"
    row.pending_enc = cipher.encrypt(str(scope.task.id), _aad(row, "pending_enc"))
    row.selection_chat_id = scope.chat_id
    row.pending_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    return {"status": "selecting", "prompt": SELECTION_PROMPT, "remote_revoked": remote_revoked}


async def choose_authorization(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    choice: str,
    *,
    selection_task_id: int | None = None,
) -> dict[str, Any]:
    row, secret = await _row(session, cipher, scope)
    if (
        row.status != "selecting"
        or row.selection_chat_id != scope.chat_id
        or not row.pending_expires_at
        or row.pending_expires_at <= datetime.now(UTC)
    ):
        raise PersonalError("selection_required")
    if selection_task_id is not None and (
        not row.pending_enc
        or cipher.decrypt(row.pending_enc, _aad(row, "pending_enc")) != str(selection_task_id)
    ):
        raise PersonalError("selection_required")
    levels = {"1": "all", "2": "all_except_send", "3": "messages_readonly"}
    if choice not in levels:
        raise PersonalError("selection_required")
    level = levels[choice]
    available = (
        list(permissions.MESSAGE_SCOPES)
        if level == "messages_readonly"
        else await permissions.app_user_scopes(row.app_id, secret, http=_http)
    )
    if level == "all" and not {"im:message", "im:message.send_as_user"}.issubset(available):
        raise PersonalError("app_send_permission_missing")
    if level == "messages_readonly" and not (
        permissions.MESSAGE_SCOPES - {"offline_access"}
    ).issubset(available):
        raise PersonalError("app_message_permission_missing")
    selected = permissions.select_scopes(level, available)
    if not selected:
        raise PersonalError("authorization_unavailable")
    row.authorization_level = level
    row.requested_scopes = selected
    # Only this worker-owned path can select a level; MCP has no level argument.
    return await _start_authorization(row, secret, cipher)


async def _start_authorization(
    row: FeishuPersonalGrant, secret: str, cipher: Cipher
) -> dict[str, Any]:
    data = await _http(
        "POST",
        "https://accounts.feishu.cn/oauth/v1/device_authorization",
        auth=(row.app_id, secret),
        data={"client_id": row.app_id, "scope": " ".join(row.requested_scopes)},
    )
    url = data.get("verification_uri_complete", "")
    parsed = urlparse(url)
    if (
        not data.get("device_code")
        or parsed.scheme != "https"
        or parsed.hostname not in ("accounts.feishu.cn", "open.feishu.cn")
        or parsed.username
    ):
        raise PersonalError("authorization_unavailable")
    row.context_epoch = uuid.uuid4()
    row.status = "pending"
    row.token_enc = None
    row.scopes = []
    row.next_poll_at = None
    row.pending_enc = cipher.encrypt(
        jsonlib.dumps({"device_code": data["device_code"], "url": url}), _aad(row, "pending_enc")
    )
    row.pending_expires_at = datetime.now(UTC) + timedelta(
        seconds=min(1800, int(data.get("expires_in", 600)))
    )
    row.poll_interval = max(1, min(60, int(data.get("interval", 5))))
    # First check may happen immediately; subsequent polls obey the server interval.
    return {
        "status": "pending",
        "authorization_url": url,
        "expires_at": row.pending_expires_at.isoformat(),
    }


async def authorize(session: AsyncSession, cipher: Cipher, scope: Scope) -> dict[str, Any]:
    row, _ = await _row(session, cipher, scope)
    if row.status == "connected" and row.token_enc:
        return _state(row, cipher)
    if (
        row.status == "pending"
        and row.pending_enc
        and row.pending_expires_at
        and row.pending_expires_at > datetime.now(UTC)
    ):
        pending = jsonlib.loads(cipher.decrypt(row.pending_enc, _aad(row, "pending_enc")))
        return {
            **_state(row, cipher),
            "status": "pending",
            "authorization_url": pending["url"],
            "expires_at": row.pending_expires_at.isoformat(),
        }
    return {
        "retention_notice": RETENTION_NOTICE,
        "status": "selection_required",
        "prompt": "请发送连接飞书，再由本人点击授权卡片选择范围。",
    }


async def authorization_status(
    session: AsyncSession, cipher: Cipher, scope: Scope
) -> dict[str, Any]:
    row, secret = await _row(session, cipher, scope)
    if row.status != "pending" or not row.pending_enc:
        return _state(row, cipher)
    now = datetime.now(UTC)
    if not row.pending_expires_at or row.pending_expires_at <= now:
        _clear(row, "expired")
        return _state(row, cipher)
    if row.next_poll_at and row.next_poll_at > now:
        return {
            **_state(row, cipher),
            "status": "pending",
            "retry_after": max(1, int((row.next_poll_at - now).total_seconds())),
        }
    pending = jsonlib.loads(cipher.decrypt(row.pending_enc, _aad(row, "pending_enc")))
    row.next_poll_at = now + timedelta(seconds=row.poll_interval)
    data = await _http(
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": pending["device_code"],
            "client_id": row.app_id,
            "client_secret": secret,
        },
    )
    error = data.get("error")
    if error in ("authorization_pending", "slow_down"):
        if error == "slow_down":
            row.poll_interval = min(60, row.poll_interval + 5)
            row.next_poll_at = now + timedelta(seconds=row.poll_interval)
        return {**_state(row, cipher), "status": "pending", "retry_after": row.poll_interval}
    if error or not data.get("access_token"):
        _clear(row, "expired")
        raise PersonalError("authorization_failed")
    info = await _http(
        "GET",
        BASE + "/authen/v1/user_info",
        headers={"Authorization": "Bearer " + data["access_token"]},
    )
    identity = info.get("data") or {}
    if (
        info.get("code") != 0
        or identity.get("user_id") != row.platform_user_id
        or identity.get("open_id") != row.open_id
        or identity.get("tenant_key") != row.tenant_key
    ):
        _clear(row)
        raise PersonalError("identity_mismatch")
    save_tokens(cipher, row, data, secret)
    return _state(row, cipher)


async def revoke_grant(
    session: AsyncSession, bot_id: uuid.UUID, user_id: uuid.UUID, cipher: Cipher | None = None
) -> dict[str, Any]:
    await _lock(session, bot_id, user_id)
    row = (
        await session.scalars(
            select(FeishuPersonalGrant)
            .where(FeishuPersonalGrant.bot_id == bot_id, FeishuPersonalGrant.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    remote_revoked = True
    if row:
        remote_revoked = row.remote_revoked
        if row.token_enc:
            remote_revoked = False
            if cipher is not None:
                from coreman.core.db.models import Bot

                bot = await session.get(Bot, bot_id)
                try:
                    if bot is None:
                        raise ValueError("bot_unavailable")
                    app_id, secret = app_credentials(cipher, bot)
                    if app_id == row.app_id:
                        remote_revoked = await _revoke_remote(cipher, row, secret)
                except ValueError:
                    pass
        row.remote_revoked = remote_revoked
        _clear(row)
    return {"status": "revoked", "remote_revoked": remote_revoked}


async def revoke_authorization(
    session: AsyncSession, cipher: Cipher, scope: Scope
) -> dict[str, Any]:
    return await revoke_grant(session, scope.bot.id, scope.user_id, cipher)


def _check_read_scope(row: FeishuPersonalGrant, path: str) -> None:
    if row.authorization_level == "legacy_readonly":
        return
    if path == "/im/v1/messages/search":
        required = "search:message"
    elif path.startswith("/im/v1/messages"):
        required = "im:message:readonly"
    elif path == "/vc/v1/meetings/search":
        required = "vc:meeting.search:read"
    elif path.startswith("/vc/v1/meetings/"):
        required = "vc:meeting:readonly"
    elif path.startswith("/vc/v1/notes/"):
        required = "vc:note:read"
    elif path == "/minutes/v1/minutes/search":
        required = "minutes:minutes.search:read"
    elif path.endswith("/artifacts"):
        required = "minutes:minutes.artifacts:read"
    elif path.startswith("/minutes/"):
        required = "minutes:minutes.basic:read"
    else:
        required = "docx:document:readonly"
    if required not in set(row.requested_scopes or []) & set(row.scopes or []):
        raise PersonalError("selected_permission_missing")


async def api_request(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    method: str,
    path: str,
    *,
    params: Any = None,
    json: Any = None,
) -> dict[str, Any]:
    sending = method == "POST" and path == "/im/v1/messages"
    allowed = (
        sending
        or (
            method == "POST"
            and path
            in ("/im/v1/messages/search", "/vc/v1/meetings/search", "/minutes/v1/minutes/search")
        )
        or (
            method == "GET"
            and re.fullmatch(
                r"/(?:im/v1/messages(?:/mget)?|vc/v1/(?:meetings|notes)/[A-Za-z0-9_-]+"
                r"|minutes/v1/minutes/[A-Za-z0-9_-]+(?:/artifacts)?"
                r"|docx/v1/documents/[A-Za-z0-9_-]+/raw_content)",
                path,
            )
            is not None
        )
    )
    if not allowed:
        raise PersonalError("invalid_tool_or_arguments")
    row, secret = await _row(session, cipher, scope)
    if row.status != "connected" or not row.token_enc:
        raise PersonalError("authorization_required")
    if sending and (
        row.authorization_level != "all"
        or not {"im:message", "im:message.send_as_user"}.issubset(
            set(row.scopes or []) & set(row.requested_scopes or [])
        )
    ):
        raise PersonalError("sending_not_authorized")
    if row.authorization_level == "messages_readonly" and not path.startswith("/im/v1/messages"):
        raise PersonalError("outside_selected_authorization")
    if row.authorization_level not in (
        "legacy_readonly",
        "messages_readonly",
        "all_except_send",
        "all",
    ):
        raise PersonalError("outside_selected_authorization")
    if not sending:
        _check_read_scope(row, path)
    tokens = jsonlib.loads(cipher.decrypt(row.token_enc, _aad(row, "token_enc")))
    if not row.expires_at or row.expires_at <= datetime.now(UTC) + timedelta(seconds=60):
        if not tokens.get("refresh_token"):
            _clear(row, "expired")
            raise PersonalError("authorization_required")
        data = await _http(
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": row.app_id,
                "client_secret": secret,
            },
        )
        if data.get("error") or not data.get("access_token"):
            _clear(row, "expired")
            raise PersonalError("authorization_required")
        data.setdefault("refresh_token", tokens["refresh_token"])
        save_tokens(cipher, row, data, secret)
        tokens = data
    if sending and not {"im:message", "im:message.send_as_user"}.issubset(set(row.scopes or [])):
        raise PersonalError("sending_not_authorized")
    if not sending:
        _check_read_scope(row, path)
    response = await _http(
        method,
        BASE + path,
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        params=params,
        json=json,
    )
    if response.get("code") != 0:
        # Do not forward upstream message text or request context to the model.
        if response.get("code") in (99991663, 99991668, 99991671, 99991677):
            _clear(row, "expired")
            raise PersonalError("authorization_required")
        raise PersonalError("feishu_read_failed")

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            for sensitive in (tokens.get("access_token"), tokens.get("refresh_token"), secret):
                if sensitive:
                    value = value.replace(sensitive, "[redacted]")
            return value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    return cast(dict[str, Any], redact(response.get("data") or {}))
