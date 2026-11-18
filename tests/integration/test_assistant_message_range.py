"""授权的消息范围：只群聊时少申请单聊的读权限、旧授权作废、盯消息替本人查授权结果并不碰单聊。"""

from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import message_range
from coreman.core.db.models import FeishuPersonalGrant, OutboxItem
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import permissions, revocation
from coreman.core.feishu_personal import service as personal
from coreman.core.timeutils import utcnow
from coreman.runtime.worker import assistant_range
from tests.integration.lifecycle_helpers import CIPHER
from tests.integration.watch_helpers import (
    TENANT,
    FakeFeishuApi,
    Watched,
    assistant,
    inbox_rows,
    install,
    primed,
    tick,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
TEAM = {"chat_id": "oc_team", "name": "项目群", "chat_mode": "group"}
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}
APP_SCOPES = [
    "offline_access",
    "im:message",
    "im:message.send_as_user",
    "im:message:readonly",
    "im:chat:read",
    permissions.GROUP_READ,
    permissions.P2P_READ,
]


def authorizing(monkeypatch: pytest.MonkeyPatch, who: Watched) -> tuple[FakeFeishuApi, list[Any]]:
    """读接口之外再接上设备授权、换令牌与身份核对；换到的令牌只带群聊那一项。"""
    api = install(monkeypatch)
    revoked: list[Any] = []
    read = api.http

    async def http(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url.endswith("/oauth/v1/device_authorization"):
            return {
                "device_code": "dc-1",
                "verification_uri_complete": "https://accounts.feishu.cn/verify?c=1",
                "expires_in": 600,
                "interval": 5,
            }
        if url == personal.TOKEN_URL and (kwargs.get("data") or {}).get("device_code"):
            scope = " ".join(permissions.narrow(APP_SCOPES, "group"))
            return {
                "access_token": "u-new",
                "refresh_token": "r-new",
                "expires_in": 7200,
                "scope": scope,
            }
        if url.endswith("/authen/v1/user_info"):
            identity = {"user_id": who.pid, "open_id": who.open_id, "tenant_key": TENANT}
            return {"code": 0, "data": identity}
        return await read(method, url, **kwargs)

    async def revoke(_app: str, _secret: str, tokens: dict[str, Any]) -> bool:
        revoked.append(tokens)
        return True

    async def app_scopes(*_: Any, **__: Any) -> list[str]:
        return list(APP_SCOPES)

    monkeypatch.setattr(personal, "_http", http)
    monkeypatch.setattr(revocation, "revoke_tokens", revoke)
    monkeypatch.setattr(permissions, "app_user_scopes", app_scopes)
    return api, revoked


async def grant_row(session: AsyncSession, who: Watched) -> FeishuPersonalGrant:
    found = await session.get(
        FeishuPersonalGrant, (who.bot.id, who.owner.id), populate_existing=True
    )
    assert found is not None
    return found


async def test_group_only_reauthorizes_without_direct_chats_and_the_watch_finishes_it(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    who = await assistant(db_session)
    api, revoked = authorizing(monkeypatch, who)
    base = utcnow().replace(microsecond=0)
    await primed(db_session, who, base - timedelta(days=1), head=["oc_team", "oc_li"])
    url = await message_range.change(db_session, CIPHER, who.bot, "group")
    await db_session.commit()
    assert url.startswith("https://accounts.feishu.cn/")
    grant = await grant_row(db_session, who)
    assert (grant.message_range, grant.status) == ("group", "pending")
    assert permissions.GROUP_READ in grant.requested_scopes
    assert permissions.P2P_READ not in grant.requested_scopes
    assert [tokens["access_token"] for tokens in revoked] == [who.token]
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.bot_id == who.bot.id))
    assert card is not None and card.target == {"chat_id": who.chat_id}
    factory = make_session_factory(db_engine)
    # 本人还没点授权：这一轮只查授权，不轮询、不算失败。
    pending = personal._http

    async def not_yet(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url == personal.TOKEN_URL and (kwargs.get("data") or {}).get("device_code"):
            return {"error": "authorization_pending"}
        return await pending(method, url, **kwargs)

    monkeypatch.setattr(personal, "_http", not_yet)
    await tick(factory, base)
    row = await watch_row(db_session, who)
    assert (row.state, row.last_error, row.failures) == ("idle", "authorization_pending", 0)
    # 授权完成：查到之后这一轮接着轮询。
    monkeypatch.setattr(personal, "_http", pending)
    row.next_poll_at = base
    grant = await grant_row(db_session, who)
    grant.next_poll_at = None
    await db_session.commit()
    await tick(factory, base + timedelta(seconds=5))
    grant = await grant_row(db_session, who)
    assert grant.status == "connected"
    assert message_range.view(grant) == {
        "message_range": "group",
        "status": "connected",
        "enforced": True,
    }
    # 之后的轮询只读群聊，单聊连列表里都滤掉了。
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_li", base + timedelta(seconds=10))
    api.say("oc_team", "om_team", base + timedelta(seconds=20))
    row = await watch_row(db_session, who)
    row.next_poll_at = base
    await db_session.commit()
    await tick(factory, base + timedelta(seconds=30))
    assert [str(item.message_id) for item in await inbox_rows(db_session, who)] == ["om_team"]
    assert api.read("oc_li") == []


async def test_feishu_granting_the_other_kind_is_reported(db_session: AsyncSession) -> None:
    who = await assistant(db_session)
    grant = await grant_row(db_session, who)
    grant.message_range = "group"
    grant.scopes = [permissions.GROUP_READ, permissions.P2P_READ]
    assert message_range.view(grant)["enforced"] is False


async def test_the_card_changes_the_range_only_for_the_owner(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    who = await assistant(db_session)
    changed: list[str] = []

    async def change(_s: Any, _c: Any, _bot: Any, choice: str, *, send: bool = True) -> str:
        assert send is False
        changed.append(choice)
        return "https://accounts.feishu.cn/verify?c=9"

    monkeypatch.setattr(message_range, "change", change)
    card = assistant_range.card(CIPHER, who.bot.id, 7, time.time(), "all")
    buttons = [item for item in card["body"]["elements"] if item["tag"] == "button"]
    assert [button["text"]["content"] for button in buttons] == ["只群聊", "只单聊"]
    value = buttons[0]["behaviors"][0]["value"]
    ctx = SimpleNamespace(cipher=CIPHER, log=SimpleNamespace(warning=lambda *a, **k: None))

    def inbound(sender: str) -> Any:
        return SimpleNamespace(
            id=1,
            chat_id=who.chat_id,
            reply_context={"message_id": "om_card"},
            sender_platform_user_id=sender,
        )

    handle = assistant_range.handle
    assert await handle(db_session, ctx, who.bot, inbound("pid_someone"), value) == "ignored"  # type: ignore[arg-type]
    forged = {**value, "level": value["level"].replace(".group.", ".p2p.")}
    assert await handle(db_session, ctx, who.bot, inbound(who.pid), forged) == "ignored"  # type: ignore[arg-type]
    assert await handle(db_session, ctx, who.bot, inbound(who.pid), value) == "range_changed"  # type: ignore[arg-type]
    assert changed == ["group"]
