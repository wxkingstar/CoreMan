"""企微 open_userid 解密：自建应用 `batch/openuserid_to_userid`。

进程内正负缓存：成功 24 小时、失败 5 分钟；应用行 60 秒重读一次。解出的映射由
identity.resolve_speaker 写回 `user_identities.open_id`，下次连本模块都不用叫。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.crypto import Cipher
from coreman.core.db.models import PlatformApp
from coreman.core.logging import get_logger
from coreman.core.platforms.wecom import WeComClient, WeComError

log = get_logger(__name__)
SECRET_AAD = "platform_apps.secret_enc"
_PREFERRED = ("login", "notify")


class OpenUseridResolver:
    """一个 worker 进程一份：缓存企微应用与解出的映射，别让每条群消息都打一次企微。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: Cipher,
        *,
        http: httpx.AsyncClient | None = None,
        success_ttl: float = 86400.0,
        fail_ttl: float = 300.0,
        app_ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = session_factory
        self._cipher = cipher
        self._http = http
        self.success_ttl, self.fail_ttl, self.app_ttl = success_ttl, fail_ttl, app_ttl
        self.clock = clock
        self.calls = 0
        self._ok: dict[str, tuple[str, float]] = {}
        self._failed: dict[str, float] = {}
        self._app: tuple[WeComClient | None, float] | None = None
        self._app_key: tuple[uuid.UUID, str] | None = None

    async def _pick(self) -> PlatformApp | None:
        """选一个能调接口的自建应用：优先 login，其次 notify，同级取最早建的那个。"""
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        select(PlatformApp)
                        .where(PlatformApp.platform == "wecom", PlatformApp.enabled.is_(True))
                        .order_by(PlatformApp.created_at, PlatformApp.id)
                    )
                )
                .scalars()
                .all()
            )
        for cap in _PREFERRED:
            app = next((a for a in rows if cap in (a.capabilities or []) and a.corp_id), None)
            if app is not None:
                return app
        return None

    async def _client(self) -> WeComClient | None:
        """当前该用哪个企微客户端。60 秒重读一次应用行，「没有可用应用」也缓存（值为 None）。

        应用行没变就连客户端一起留着：`WeComClient` 自带连接池，每分钟换一个既白白重解一次
        secret，也会把上一个池丢在那儿没人关（在途请求还拿着它，不能就地 aclose）。
        取应用这一步失败（库抖动、secret 解不开）当成「暂时没有可用应用」，绝不往上抛——
        这条路只是想给发言者补个名字，不该让整轮对话陪葬。
        """
        now = self.clock()
        if self._app is not None and self._app[1] > now:
            return self._app[0]
        try:
            app = await self._pick()
            if app is None:
                client: WeComClient | None = None
            elif self._app is not None and self._app_key == (app.id, app.secret_enc):
                client = self._app[0]
            else:
                secret = self._cipher.decrypt(app.secret_enc, SECRET_AAD)
                client = WeComClient(app.corp_id or "", secret, http=self._http)
        except Exception as exc:  # noqa: BLE001
            log.warning("openuserid_app_unavailable", reason=type(exc).__name__)
            app, client = None, None
        self._app_key = (app.id, app.secret_enc) if app is not None else None
        self._app = (client, now + self.app_ttl)
        return client

    async def resolve(self, open_userid: str) -> str | None:
        """密文 id → 明文 userid；解不出返回 None（负缓存 5 分钟，绝不抛错拦住这轮对话）。"""
        now = self.clock()
        hit = self._ok.get(open_userid)
        if hit is not None and hit[1] > now:
            return hit[0]
        if self._failed.get(open_userid, 0.0) > now:
            return None
        client = await self._client()
        if client is None:
            self._failed[open_userid] = now + self.fail_ttl
            return None
        self.calls += 1
        try:
            mapping = await client.openuserid_to_userid([open_userid])
        except (WeComError, httpx.HTTPError) as exc:
            log.warning("openuserid_resolve_failed", reason=type(exc).__name__)
            self._failed[open_userid] = now + self.fail_ttl
            return None
        userid = mapping.get(open_userid)
        if not userid:
            self._failed[open_userid] = now + self.fail_ttl
            return None
        self._ok[open_userid] = (userid, now + self.success_ttl)
        return userid
