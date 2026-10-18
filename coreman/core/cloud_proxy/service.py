"""阿里云签名代理的一次调用：解析、判定、（高风险时）核销 Tower 令牌、签名转发、审计。

Agent 手里没有 AK/SK，只能把未签名的业务参数交给这里；能不能发、要不要审批都由服务端决定。
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.cloud_proxy import aliyun, risk, tower_gate
from coreman.core.cloud_proxy.policy import Capability
from coreman.core.config import Settings
from coreman.core.db.models import Bot, Task, User
from coreman.core.db.models.cloud_proxy import CloudCall, CloudPendingApproval
from coreman.core.systems_catalog.fetch import registered_transport

TIMEOUT_SECONDS = 10
MAX_RESPONSE_CHARS = 100_000
# 读响应时的字节上限：截断按字符算，utf-8 最多 4 字节一个字符，再大的响应也没必要读进内存。
MAX_RESPONSE_BYTES = MAX_RESPONSE_CHARS * 4
REDACTED = "[REDACTED]"
_RESERVED_FOLDED = frozenset(p.casefold() for p in aliyun.RESERVED_PARAMS)
# 阿里云 RPC 参数名只有 ASCII 字母数字，列表用 . 下标（如 Tag.1.Key）；空白、控制字符、
# 非 ASCII 一律不收，免得网关按大小写不敏感或去空白的规则把它认成另一个参数
_PARAM_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._]*")
_ACTION_NAME = re.compile(r"[A-Za-z]+")

# 测试换成假的阿里云；生产固定只连登记的 endpoint。
transport_factory: Callable[[httpx.URL], httpx.AsyncBaseTransport] = registered_transport


def aliyun_client(endpoint: str) -> httpx.AsyncClient:
    """只能连到这个 endpoint 的客户端：不跟随重定向、不吃环境里的代理变量。"""
    url = httpx.URL(f"https://{endpoint}/")
    return httpx.AsyncClient(
        transport=transport_factory(url),
        follow_redirects=False,
        trust_env=False,
        timeout=TIMEOUT_SECONDS,
    )


ClientFactory = Callable[[str], httpx.AsyncClient]


@dataclass(frozen=True)
class ProxyScope:
    """已通过能力令牌与 task_scope 复核的本轮身份，以及路由的原始转义路径。"""

    capability: Capability
    task: Task
    bot: Bot
    user: User
    raw_path: str


class _BadRequest(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def error(http_status: int, code: str, message: str = "", **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": code, **extra}
    if message:
        body["message"] = message
    return JSONResponse(body, status_code=http_status)


def parse_body(raw: bytes) -> dict[str, str]:
    """把收到的字节解析一次；之后判定、签名都用这一个 dict，保证转发的就是判定过的。"""
    try:
        text = raw.decode("utf-8")
        pairs = urllib.parse.parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=1000,
        )
    except ValueError:
        # UnicodeDecodeError 也是 ValueError
        raise _BadRequest("bad_body", "请求体必须是 utf-8 的 x-www-form-urlencoded") from None
    params: dict[str, str] = {}
    folded: set[str] = set()
    for key, value in pairs:
        if not _PARAM_NAME.fullmatch(key):
            raise _BadRequest("bad_param_name", "参数名只能是 ASCII 字母、数字、点和下划线")
        # 同名或仅大小写不同的参数到了阿里云取哪个不确定（读动作不跑参数白名单，
        # 一个 action= 就可能把读变成写），风险判定只看得到一个值，只能拒绝
        if key.casefold() in folded:
            raise _BadRequest("duplicate_param", f"参数 {key} 出现了多次")
        folded.add(key.casefold())
        params[key] = value
    reserved = sorted(k for k in params if k.casefold() in _RESERVED_FOLDED)
    if reserved:
        raise _BadRequest("reserved_param", f"公共参数由代理生成，不能自带：{', '.join(reserved)}")
    if not params.get("Action"):
        raise _BadRequest("missing_action", "缺少 Action")
    return params


def redact(text: str, access_key_id: str) -> str:
    if access_key_id:
        text = text.replace(access_key_id, REDACTED)
    return text[:MAX_RESPONSE_CHARS]


def _now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class _Upstream:
    status: int
    text: str
    content_type: str


class _Signer:
    def __init__(self, settings: Settings, http: ClientFactory) -> None:
        assert settings.aliyun_proxy_access_key_id and settings.aliyun_proxy_access_key_secret
        self._ak = settings.aliyun_proxy_access_key_id.get_secret_value()
        self._sk = settings.aliyun_proxy_access_key_secret.get_secret_value()
        self._http = http
        self._policy = settings.cloud_proxy_policy

    @property
    def access_key_id(self) -> str:
        return self._ak

    async def send(self, product: aliyun.Product, params: dict[str, str]) -> _Upstream:
        endpoint = aliyun.endpoint_for(product, self._policy)
        if endpoint is None:
            raise ValueError("endpoint not configured")
        body = aliyun.sign_v1(
            params,
            version=product.version,
            access_key_id=self._ak,
            secret=self._sk,
            nonce=uuid.uuid4().hex,
            timestamp=_now_stamp(),
        )
        # httpx 的 timeout 按阶段计、每读一块重置；慢吞吞吐字节的上游要靠总时限兜住
        async with asyncio.timeout(TIMEOUT_SECONDS), self._http(endpoint) as client:
            async with client.stream(
                "POST",
                f"https://{endpoint}/",
                content=body.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                raw = bytearray()
                async for chunk in resp.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        break
                return _Upstream(
                    resp.status_code,
                    bytes(raw[:MAX_RESPONSE_BYTES]).decode("utf-8", errors="replace"),
                    resp.headers.get("content-type", ""),
                )


class _Reader:
    """风险判定用的读：同一个签名客户端，只准读动作；任何不正常都抛出，让判定落到高风险。"""

    def __init__(self, signer: _Signer) -> None:
        self._signer = signer

    async def call(self, product: str, action: str, params: dict[str, str]) -> dict[str, Any]:
        p = aliyun.PRODUCTS[product]
        if aliyun.classify(p, action) != "read":
            raise ValueError("risk reader only reads")
        up = await self._signer.send(p, {**params, "Action": action})
        if up.status != 200:
            raise ValueError("read failed")
        data = json.loads(up.text)
        if not isinstance(data, dict):
            raise ValueError("unexpected response")
        return data


def _forwarded(up: _Upstream, access_key_id: str) -> Response:
    text = redact(up.text, access_key_id)
    ctype = up.content_type if up.content_type.startswith(("application/json", "text/")) else ""
    if up.status >= 500:
        # 5xx 不透传状态：Agent 会把它当作代理自身故障；不重试，写操作可能已生效
        return error(502, "aliyun_error", "阿里云返回服务端错误", status=up.status, body=text)
    return Response(text, status_code=up.status, media_type=ctype or "text/plain")


async def _mark_executed(
    session: AsyncSession, scope: ProxyScope, approval_id: str, request_hash: str
) -> None:
    rows = (
        await session.scalars(
            select(CloudPendingApproval)
            .where(
                CloudPendingApproval.bot_id == scope.bot.id,
                or_(
                    CloudPendingApproval.approval_id == approval_id,
                    CloudPendingApproval.request_hash == request_hash,
                ),
                CloudPendingApproval.status.in_(("pending", "approved")),
            )
            .order_by(CloudPendingApproval.created_at.desc())
        )
    ).all()
    # 令牌只绑定到 bot：优先认回调已写上 approval_id 的那条，其次本任务按哈希挂起的，
    # 最后才是同一 bot 其他任务的同一请求，免得把别的任务的挂起单收掉
    row = (
        next((r for r in rows if r.approval_id == approval_id), None)
        or next((r for r in rows if r.task_id == scope.capability.task_id), None)
        or (rows[0] if rows else None)
    )
    if row is None:
        return
    await session.execute(
        update(CloudPendingApproval)
        .where(CloudPendingApproval.external_id == row.external_id)
        .values(status="executed", approval_id=approval_id)
    )


async def _pending_for(
    session: AsyncSession, scope: ProxyScope, product_key: str, action: str, req_hash: str
) -> str:
    cap = scope.capability
    # 同一任务重复提交同一请求复用挂起单，不让循环重试把表撑大
    existing = await session.scalar(
        select(CloudPendingApproval.external_id)
        .where(
            CloudPendingApproval.task_id == cap.task_id,
            CloudPendingApproval.bot_id == scope.bot.id,
            CloudPendingApproval.request_hash == req_hash,
            CloudPendingApproval.status == "pending",
        )
        .order_by(CloudPendingApproval.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    external_id = f"cm-{cap.task_id}-{secrets.token_hex(8)}"
    session.add(
        CloudPendingApproval(
            external_id=external_id,
            task_id=cap.task_id,
            bot_id=scope.bot.id,
            user_id=scope.user.id,
            # 定时任务没有会话键时留空，恢复时据 task 判断来源
            session_key=cap.session_key or scope.task.session_key or "",
            origin_event_id=cap.event_id,
            origin_relay_session_id=str(cap.relay_session_id) if cap.relay_session_id else None,
            product=product_key,
            action=action,
            request_hash=req_hash,
        )
    )
    return external_id


async def handle(
    session: AsyncSession,
    scope: ProxyScope,
    product_key: str,
    raw_body: bytes,
    approval_token: str | None,
    settings: Settings,
    http: ClientFactory = aliyun_client,
) -> Response:
    started = time.monotonic()
    cap = scope.capability
    audit: dict[str, Any] = {
        "task_id": cap.task_id,
        "bot_id": scope.bot.id,
        "user_id": scope.user.id,
        "product": product_key,
    }

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def call_row(
        *,
        action: str,
        risk_level: str,
        outcome: str,
        status_code: int | None,
        approval_id: str | None = None,
        request_hash: str | None = None,
    ) -> CloudCall:
        return CloudCall(
            **audit,
            action=action,
            risk=risk_level,
            outcome=outcome,
            approval_id=approval_id,
            request_hash=request_hash,
            status_code=status_code,
            duration_ms=elapsed_ms(),
        )

    async def done(
        response: Response,
        *,
        action: str,
        risk_level: str,
        outcome: str,
        status_code: int | None = None,
        approval_id: str | None = None,
        request_hash: str | None = None,
    ) -> Response:
        session.add(
            call_row(
                action=action,
                risk_level=risk_level,
                outcome=outcome,
                status_code=status_code if status_code is not None else response.status_code,
                approval_id=approval_id,
                request_hash=request_hash,
            )
        )
        await session.commit()
        return response

    product = aliyun.PRODUCTS.get(product_key)
    if product is None:
        return error(403, "product_not_allowed", f"不支持的产品 {product_key}")
    if aliyun.endpoint_for(product, settings.cloud_proxy_policy) is None:
        return error(
            503,
            "product_not_configured",
            f"{product_key} 的 endpoint 未配置（CLOUD_PROXY_POLICY.{product_key}.endpoint）",
        )
    try:
        params = parse_body(raw_body)
    except _BadRequest as exc:
        return error(400, exc.code, exc.message)
    action = params["Action"]
    kind = aliyun.classify(product, action)
    if kind is None:
        # 判不出类别的按最高风险记账，便于事后排查谁在拿代理试探；动作名是 agent 给的，
        # 不回显、只存短的合法名，不让人拿它撑大审计表
        logged = action[:64] if _ACTION_NAME.fullmatch(action) else "<invalid>"
        return await done(
            error(403, "action_not_allowed", f"{product_key} 不允许这个 Action"),
            action=logged,
            risk_level="write_high",
            outcome="denied",
        )

    signer = _Signer(settings, http)
    level = "read"
    req_hash: str | None = None
    approval_id: str | None = None
    redeemed_id: int | None = None
    if kind == "write":
        # 高风险写要以发言者的登录名作 Tower 的 requester，没有就永远批不下来；在读阿里云判定风险、
        # 建挂起单之前就拒，给一个说得清的原因，而不是让 Agent 拿着空 requester 去 Tower 碰壁
        if not scope.user.login_name:
            return await done(
                error(
                    403,
                    "no_login_name",
                    "当前发言者在 CoreMan 里没有登录名（login_name），云服务写操作无法提交审批；"
                    "请管理员先在成员资料里补上登录名",
                ),
                action=action,
                risk_level="write_high",
                outcome="denied",
            )
        assessment = await risk.assess(
            product_key,
            action,
            params,
            _Reader(signer),
            now=datetime.now(UTC),
            policy=settings.cloud_proxy_policy,
        )
        level = "write_high" if assessment.high else "write_low"
        if assessment.high:
            target = f"aliyun-{product_key}"
            caller = scope.bot.bot_key
            url = settings.public_base_url.rstrip("/") + scope.raw_path
            # 哈希路径取自发给 Agent 的 url：Agent 拿这个 url 提交审批，前面有路径前缀时两边也一致；
            # 无 query、收到的原始字节
            req_hash = tower_gate.request_hash_parts(
                "POST", target, urllib.parse.urlsplit(url).path, "", raw_body, caller
            )
            if not approval_token:
                external_id = await _pending_for(session, scope, product_key, action, req_hash)
                body = {
                    "error": "approval_required",
                    "target": target,
                    "caller": caller,
                    "requester": scope.user.login_name,
                    "external_id": external_id,
                    "url": url,
                    "risk_reasons": list(assessment.reasons),
                    "message": "该操作为高风险，需要先经 Tower 审批；"
                    "批准后用同一份请求体带上 X-Tower-Approval 头重发",
                }
                return await done(
                    JSONResponse(body, status_code=428),
                    action=action,
                    risk_level=level,
                    outcome="approval_required",
                    request_hash=req_hash,
                )
            key = settings.tower_target_keys[product_key].get_secret_value()
            try:
                approval_id = await asyncio.to_thread(
                    tower_gate.redeem, settings.tower_url, key, approval_token, req_hash, caller
                )
            except tower_gate.TowerGateError as exc:
                return await done(
                    error(exc.status, exc.code),
                    action=action,
                    risk_level=level,
                    outcome="denied" if exc.status == 403 else "unreachable",
                    request_hash=req_hash,
                )
            # 令牌已消耗：出站前先把审计行和 executed 落盘提交，进程在出站中途被杀也留得下记录；
            # 也不能把挂起单的行锁带到出站期间，回调会被堵住。outcome 先占位，出站后再改
            redeemed = call_row(
                action=action,
                risk_level=level,
                outcome="unreachable",
                status_code=None,
                approval_id=approval_id,
                request_hash=req_hash,
            )
            session.add(redeemed)
            await session.flush()
            redeemed_id = redeemed.id
            await _mark_executed(session, scope, approval_id, req_hash)
            await session.commit()

    async def finish(
        response: Response, *, outcome: str, status_code: int | None = None
    ) -> Response:
        if redeemed_id is None:
            return await done(
                response,
                action=action,
                risk_level=level,
                outcome=outcome,
                status_code=status_code,
            )
        await session.execute(
            update(CloudCall)
            .where(CloudCall.id == redeemed_id)
            .values(
                outcome=outcome,
                status_code=status_code if status_code is not None else response.status_code,
                duration_ms=elapsed_ms(),
            )
        )
        await session.commit()
        return response

    try:
        up = await signer.send(product, params)
    except (httpx.HTTPError, TimeoutError, ValueError):
        # ValueError 来自固定主机 transport 的构造校验
        return await finish(error(502, "aliyun_unreachable", "连不上阿里云"), outcome="unreachable")
    return await finish(
        _forwarded(up, signer.access_key_id),
        outcome="ok" if 200 <= up.status < 300 else "http_error",
        status_code=up.status,
    )
