"""读一篇群里的文档时用哪个身份：A 层应用身份（先证明本群看得到），B 层提问人放行。

A 层只靠「应用读得到」不够：应用读得到的还有共享给它所在其他群的文档、被单独加为文档应用
的文档。所以先查本群是不是协作者、或链接分享对本群成员是否开放，证明不了就不用应用身份读。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import BotAllowedUser, FeishuDocConsent, FeishuPersonalGrant
from coreman.core.feishu_docs.links import Link
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.feishu_personal import endpoints, policy
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.platforms.feishu import FeishuClient, FeishuError

# 应用身份只用这几个只读接口（与个人工具共用登记表里的路径与参数校验）。
APP_ENDPOINTS = frozenset(
    {
        "docx.raw",
        "wiki.node",
        "sheets.query",
        "sheets.read",
        "bitable.tables",
        "bitable.fields",
        "bitable.search",
    }
)
# 每种文档读正文/清单要用的接口：提问人的档位与权限要够得着才谈得上放行。
READ_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "docx": ("docs.fetch",),
    "wiki": ("wiki.node", "docs.fetch"),
    "sheets": ("sheets.query", "sheets.read"),
    "base": ("bitable.tables", "bitable.search"),
}
SHARE_ENDPOINT = "drive.share"
_CHAT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ANYONE = frozenset({"anyone_readable", "anyone_editable"})
_TENANT = frozenset({"tenant_readable", "tenant_editable"})


def app_client(cipher: Cipher, scope: DocsScope) -> FeishuClient:
    try:
        app_id, secret = policy.app_credentials(cipher, scope.bot)
    except ValueError:
        raise PersonalError("feishu_app_unavailable") from None
    return FeishuClient(app_id, secret)


class AppCall:
    """与个人工具的 `Call` 同一个调用约定，但以应用身份调用，只放行 `APP_ENDPOINTS`。"""

    def __init__(self, client: FeishuClient) -> None:
        self.client = client

    async def __call__(
        self,
        key: str,
        *,
        path: dict[str, str] | None = None,
        params: Any = None,
        json: Any = None,
    ) -> dict[str, Any]:
        if key not in APP_ENDPOINTS:
            raise PersonalError("invalid_tool_or_arguments")
        endpoint = endpoints.ENDPOINTS[key]
        try:
            built = endpoints.build(endpoint, **(path or {}))
        except ValueError:
            raise PersonalError("invalid_tool_or_arguments") from None
        kwargs: dict[str, Any] = {"params": params}
        if json is not None:
            kwargs["json"] = json
        try:
            body = await self.client.call(endpoint.method, "/open-apis" + built, **kwargs)
        except FeishuError as exc:
            raise PersonalError("app_read_failed", upstream_code=exc.code) from None
        data = body.get("data")
        return data if isinstance(data, dict) else {}


async def chat_can_see(client: FeishuClient, link: Link, chat_id: str) -> bool:
    """本群看得到这篇文档：本群是协作者，或链接分享对本群成员开放。查不了一律算看不到。"""
    if not _CHAT_ID.fullmatch(chat_id):
        return False
    base = f"/open-apis/drive/v1/permissions/{link.token}"
    params = {"type": link.permission_type}
    try:
        members = await client.call("GET", base + "/members", params=params)
        items = (members.get("data") or {}).get("items") or []
        if any(
            isinstance(item, dict)
            and item.get("member_type") == "openchat"
            and item.get("member_id") == chat_id
            for item in items
        ):
            return True
    except FeishuError:
        pass
    try:
        public = await client.call("GET", base + "/public", params=params)
    except FeishuError:
        return False
    setting = (public.get("data") or {}).get("permission_public") or {}
    entity = setting.get("link_share_entity") if isinstance(setting, dict) else None
    if entity in _ANYONE:
        return True
    if entity not in _TENANT:
        return False
    # 组织内可读：外部群里有组织外的人，他们打不开，不能替他们读。
    try:
        chat = await client.call("GET", f"/open-apis/im/v1/chats/{chat_id}")
    except FeishuError:
        return False
    return (chat.get("data") or {}).get("external") is False


@dataclass(frozen=True)
class Requester:
    """连接过飞书的提问人：个人工具的 Scope 与他当前的档位、权限。"""

    scope: policy.Scope
    level: str
    granted: frozenset[str]

    def reaches(self, *keys: str) -> bool:
        return all(
            endpoints.denied(endpoints.ENDPOINTS[key], self.level, self.granted) is None
            for key in keys
        )


async def requester(session: AsyncSession, scope: DocsScope) -> Requester | None:
    """提问人连接过飞书、身份对得上、仍是本机器人的使用者时，返回他的个人授权。"""
    if scope.speaker_user_id is None:
        return None
    grant = await session.get(
        FeishuPersonalGrant, (scope.bot.id, scope.speaker_user_id), populate_existing=True
    )
    if (
        grant is None
        or grant.status != "connected"
        or not grant.token_enc
        or grant.platform_user_id != scope.event.sender_platform_user_id
        or grant.open_id != scope.event.sender_open_id
    ):
        return None
    allowed = list(
        await session.scalars(
            select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == scope.bot.id)
        )
    )
    if allowed and scope.speaker_user_id not in allowed:
        return None
    personal = policy.Scope(
        scope.task,
        scope.bot,
        scope.speaker_user_id,
        grant.tenant_key,
        grant.app_id,
        grant.open_id,
        grant.platform_user_id,
        scope.chat_id,
        scope.event,
    )
    granted = frozenset(grant.requested_scopes or []) & frozenset(grant.scopes or [])
    return Requester(personal, grant.authorization_level, granted)


async def approved(
    session: AsyncSession, scope: DocsScope, token: str, now: datetime | None = None
) -> FeishuDocConsent | None:
    """本人在本群对这篇文档的放行，仍在有效期内。"""
    if scope.speaker_user_id is None:
        return None
    found = await session.scalars(
        select(FeishuDocConsent)
        .where(
            FeishuDocConsent.bot_id == scope.bot.id,
            FeishuDocConsent.chat_id == scope.chat_id,
            FeishuDocConsent.user_id == scope.speaker_user_id,
            FeishuDocConsent.status == "approved",
            FeishuDocConsent.expires_at > (now or datetime.now(UTC)),
            FeishuDocConsent.tokens.any(token),  # type: ignore[arg-type]
        )
        .limit(1)
    )
    return found.first()


def allowed_tokens(scope: DocsScope) -> set[str]:
    """提问人这条消息（含引用的消息）里出现的文档 token：只有它们能请求放行。"""
    raw = scope.task.payload.get("doc_links")
    return {str(item) for item in raw} if isinstance(raw, list) else set()
