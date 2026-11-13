"""盯消息用例的公共件：一位可以轮询的助理，以及飞书读接口与令牌刷新的替身。

替身替换的是 `service._http`：授权、加锁、刷新、限频识别之后的路径都走真的代码。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import keys, sealed, settings, watch
from coreman.core.assistants.apps import SECRET_AAD
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.db.models import (
    AssistantInbox,
    AssistantWatch,
    Bot,
    FeishuPersonalGrant,
    PlatformApp,
    Task,
    User,
    UserIdentity,
)
from coreman.core.feishu_personal import ratelimit
from coreman.core.feishu_personal import service as personal
from coreman.core.feishu_personal.policy import Scope
from coreman.core.timeutils import utcnow
from tests.integration.lifecycle_helpers import CIPHER, pool_relay

APP_ID, APP_SECRET, TENANT = "cli_example", "app-secret", "tenant-example"
READ_SCOPES = ["offline_access", "im:chat:read", "im:message:readonly"]


@dataclass
class Watched:
    bot: Bot
    owner: User
    open_id: str
    chat_id: str
    pid: str
    token: str


async def assistant(
    session: AsyncSession,
    tag: str = "a",
    *,
    cold_started: bool = True,
    shift: str | None = "on",
    expired: bool = False,
) -> Watched:
    """一位可以轮询的助理。`shift="on"` 让它不看钟点一直在上班；`expired` 让访问令牌已过期。"""
    owner = User(login_name=f"watch-{tag}", display_name="张三", source="sync")
    session.add(owner)
    await session.flush()
    pid, open_id, chat_id = f"pid_{tag}", f"ou_owner_{tag}", f"oc_assistant_{tag}"
    session.add(
        UserIdentity(user_id=owner.id, platform="feishu", platform_user_id=pid, open_id=open_id)
    )
    # 所有助理共用一个应用：限频按应用计。
    app = await session.scalar(select(PlatformApp).where(PlatformApp.app_id == APP_ID))
    if app is None:
        app = PlatformApp(
            platform="feishu",
            name="个人助理-watch",
            capabilities=["assistant"],
            app_id=APP_ID,
            secret_enc=CIPHER.encrypt(APP_SECRET, SECRET_AAD),
        )
        session.add(app)
        await session.flush()
    relay = await pool_relay(session)
    bot = new_assistant_bot(
        owner,
        app,
        relay_server_id=relay.id,
        model="m",
        working_dir=f"/srv/pa/{tag}",
        credentials_enc=encrypt_json(
            CIPHER, {"app_id": APP_ID, "app_secret": APP_SECRET}, CREDENTIALS_AAD
        ),
    )
    bot.assistant_state = "active"
    session.add(bot)
    await session.flush()
    await keys.ensure_key(session, sealed.master(), owner.id)
    token = f"u-access-{tag}"
    grant = FeishuPersonalGrant(
        bot_id=bot.id,
        user_id=owner.id,
        app_id=APP_ID,
        app_fingerprint=personal._fingerprint(APP_ID, APP_SECRET),
        platform_user_id=pid,
        open_id=open_id,
        tenant_key=TENANT,
        status="connected",
        authorization_level="all",
        scopes=list(READ_SCOPES),
        requested_scopes=list(READ_SCOPES),
        expires_at=utcnow() + timedelta(hours=-1 if expired else 1),
    )
    grant.token_enc = CIPHER.encrypt(
        json.dumps({"access_token": token, "refresh_token": f"u-refresh-{tag}"}),
        personal._aad(grant, "token_enc"),
    )
    session.add(grant)
    prefs = await settings.record_identity(
        session, bot.id, open_id=open_id, tenant_key=TENANT, platform_user_id=pid, chat_id=chat_id
    )
    prefs.shift_override = shift
    prefs.cold_start_done_at = utcnow() if cold_started else None
    await session.commit()
    sealed.forget()
    return Watched(bot, owner, open_id, chat_id, pid, token)


def scope_of(who: Watched) -> Scope:
    """不带任务的 scope，与盯消息循环里拼的一样。"""
    return Scope(
        cast(Task, None), who.bot, who.owner.id, TENANT, APP_ID, who.open_id, who.pid, who.chat_id
    )


async def tokens(session: AsyncSession, who: Watched) -> dict[str, Any]:
    """库里这位所有者现在的令牌。"""
    grant = await session.get(
        FeishuPersonalGrant, (who.bot.id, who.owner.id), populate_existing=True
    )
    assert grant is not None and grant.token_enc
    found: dict[str, Any] = json.loads(
        CIPHER.decrypt(grant.token_enc, personal._aad(grant, "token_enc"))
    )
    return found


def msg(
    mid: str,
    at: datetime,
    *,
    chat: str,
    sender: str = "ou_li",
    kind: str = "user",
    text: str = "周五能给报价吗？",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "message_id": mid,
        "chat_id": chat,
        "msg_type": "text",
        "create_time": str(int(at.timestamp() * 1000)),
        "sender": {"id": sender, "id_type": "open_id", "sender_type": kind},
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
        **extra,
    }


def _since(message: dict[str, Any], floor: int) -> bool:
    """这条消息不早于 `floor`；时间读不出来的（用例故意造的坏数据）照样给出去。"""
    try:
        return int(message["create_time"]) >= floor
    except (KeyError, TypeError, ValueError):
        return True


@dataclass
class Hit:
    path: str
    params: dict[str, Any]
    token: str


@dataclass
class FakeFeishuApi:
    """飞书的读接口与令牌刷新。`chats` 按最近活跃倒序；`history` 按会话存消息（旧的在前）。

    带 `"_thread_only": True` 的消息只在话题容器里读得到，主线里没有。
    """

    chats: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)
    hits: list[Hit] = field(default_factory=list)
    refreshes: int = 0
    limited: dict[str, int] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)
    before: Callable[[Hit], Awaitable[None]] | None = None

    def say(self, chat: str, mid: str, at: datetime, **kw: Any) -> dict[str, Any]:
        item = msg(mid, at, chat=chat, **kw)
        self.history.setdefault(chat, []).append(item)
        return item

    def paths(self) -> list[str]:
        return [hit.path for hit in self.hits]

    def read(self, container: str) -> list[Hit]:
        """读过这个会话（或话题）历史的调用。"""
        return [
            hit
            for hit in self.hits
            if hit.path == "/im/v1/messages" and hit.params.get("container_id") == container
        ]

    def _wait(self, key: str) -> None:
        if key in self.limited:
            raise ratelimit.RateLimited(self.limited[key])

    async def http(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url == personal.TOKEN_URL:
            self._wait(url)
            self.refreshes += 1
            return {
                "access_token": f"u-access-r{self.refreshes}",
                "refresh_token": f"u-refresh-r{self.refreshes}",
                "expires_in": 7200,
                "scope": " ".join(READ_SCOPES),
            }
        path = url.removeprefix(personal.BASE)
        params = dict(kwargs.get("params") or {})
        bearer = str((kwargs.get("headers") or {}).get("Authorization", ""))
        hit = Hit(path, params, bearer.removeprefix("Bearer "))
        self.hits.append(hit)
        if self.before is not None:
            await self.before(hit)
        self._wait(path)
        everything = [item for items in self.history.values() for item in items]
        if path == "/im/v1/chats":
            return {"code": 0, "data": {"items": list(self.chats)}}
        if path == "/im/v1/messages/mget":
            wanted = params["message_ids"]
            return {
                "code": 0,
                "data": {"items": [m for m in everything if m["message_id"] in wanted]},
            }
        if path == "/im/v1/messages":
            container = params["container_id"]
            if container in self.failing:
                return {"code": 230001, "msg": "upstream text that must not travel"}
            if params["container_id_type"] == "thread":
                assert "start_time" not in params
                found = [m for m in everything if m.get("thread_id") == container]
                found.sort(key=lambda m: int(m["create_time"]), reverse=True)
            else:
                floor = int(params.get("start_time", "0")) * 1000
                found = [
                    m
                    for m in self.history.get(container, [])
                    if _since(m, floor) and not m.get("_thread_only")
                ]
            # 按页给：`page_token` 是下一页的起点。
            start, size = int(params.get("page_token") or 0), int(params.get("page_size") or 50)
            more = start + size < len(found)
            data: dict[str, Any] = {"items": found[start : start + size], "has_more": more}
            if more:
                data["page_token"] = str(start + size)
            return {"code": 0, "data": data}
        if path.startswith("/im/v1/chats/"):
            chat_id = path.rsplit("/", 1)[1]
            return {"code": 0, "data": {"chat_mode": self.modes.get(chat_id, "group"), "name": ""}}
        raise AssertionError((method, path))


def install(monkeypatch: pytest.MonkeyPatch) -> FakeFeishuApi:
    api = FakeFeishuApi()
    monkeypatch.setattr(personal, "_http", api.http)
    return api


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


async def primed(
    session: AsyncSession, who: Watched, since: datetime, *, head: list[str] | None = None
) -> None:
    """一行已经过了第一轮（只记基线）的盯消息状态，立刻到期；`head` 是上一轮的会话顺序。"""
    session.add(
        AssistantWatch(
            bot_id=who.bot.id,
            cursor={"since": ms(since), "chats": {}, "todo": []},
            head_enc=list(head or []),
            next_poll_at=since,
        )
    )
    await session.commit()


async def tick(factory: async_sessionmaker[AsyncSession], at: datetime) -> dict[str, int]:
    return await watch.tick(factory, CIPHER, at)


async def watch_row(session: AsyncSession, who: Watched) -> AssistantWatch:
    row = await session.get(AssistantWatch, who.bot.id, populate_existing=True)
    assert row is not None
    return row


async def inbox_rows(session: AsyncSession, who: Watched) -> list[AssistantInbox]:
    rows = list(
        await session.scalars(
            select(AssistantInbox)
            .where(AssistantInbox.bot_id == who.bot.id)
            .order_by(AssistantInbox.id)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    return rows
