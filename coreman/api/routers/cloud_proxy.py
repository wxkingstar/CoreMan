"""阿里云签名代理：Agent 交未签名的参数，CoreMan 判定风险、按需核销 Tower 令牌后签名转发。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.core.cloud_proxy import policy, service, tower_gate
from coreman.core.systems_catalog import policy as systems_policy

router = APIRouter(tags=["cloud-proxy"])
MAX_BODY = 262_144


async def _read_body(request: Request) -> bytes | None:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            return None
    return bytes(raw)


@router.post(policy.API_PATH + "/aliyun/{product}")
async def aliyun_proxy(
    product: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    # 能力令牌只发给 agent 进程；浏览器带着 Origin 来的一律不认，免得被网页借道
    if "origin" in request.headers:
        return service.error(403, "origin_not_allowed")
    settings = request.app.state.settings
    if not settings.cloud_proxy_ready:
        return service.error(503, "cloud_proxy_disabled", "云签名代理未配置")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        return service.error(401, "invalid_capability")
    try:
        capability = policy.read_capability(request.app.state.cipher, auth[7:])
    except (ValueError, KeyError, TypeError, OverflowError):
        return service.error(401, "invalid_capability")
    try:
        scope = await systems_policy.task_scope(
            session, systems_policy.Capability(capability.task_id, capability.user_id)
        )
    except ValueError:
        return service.error(403, "task_not_active")
    if scope.bot.id != capability.bot_id:
        return service.error(403, "task_not_active")
    raw = await _read_body(request)
    if raw is None:
        return service.error(413, "body_too_large")
    if request.url.query:
        # 哈希按空 query 算；带了 query 的请求批准后也核销不上，提前说清楚
        return service.error(400, "query_not_allowed")
    raw_path = request.scope.get("raw_path") or request.url.path.encode()
    return await service.handle(
        session,
        service.ProxyScope(
            capability=capability,
            task=scope.task,
            bot=scope.bot,
            user=scope.user,
            raw_path=raw_path.decode("latin-1"),
        ),
        product,
        raw,
        request.headers.get(tower_gate.HEADER, "").strip() or None,
        settings,
    )
