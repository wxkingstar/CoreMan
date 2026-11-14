"""Tower 的 agent 发送端：提交（同一外部编号再次提交即改稿）、取令牌、核销、上报结果、撤回。

接口形状已对照 Tower 服务端实现核实（见计划文末）：错误应答是 `{"error": 代码, "message": …}`。

平台既是发送端又是目标系统：个人工具的写操作登记为 `feishu-personal`，调度公共员工登记为
`assistant-dispatch`，各有各的 Key，由 `Config.target` 带着。提交时用发送端的 Key，批准后先用
发送端的 Key 取一次性令牌，再用目标系统的 Key 核销。令牌与「方法 + 路径 + 请求体 + 调用方」的
哈希绑定，请求体是暂存参数的规范化形式，所以核销通过就说明要执行的正是被批准的那份参数。

请求不跟随重定向、不读环境里的代理变量：头里带着 API Key。报错只带代码，不带地址与正文。
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from coreman.core.assistants.approvals import attachments
from coreman.core.cloud_proxy import tower_gate
from coreman.core.config import Settings

TARGET = "feishu-personal"
# 调度公共员工（`assistant_ask_bot` 且允许对方改动）的审批单走自己的目标系统。
DISPATCH_TARGET = "assistant-dispatch"
TIMEOUT_SECONDS = 15.0
# 测试换成 httpx.MockTransport。
transport: httpx.AsyncBaseTransport | None = None


class TowerError(Exception):
    """Tower 拒绝或不可用。`status` 为 503 表示没连上或应答不成形，可以稍后重试。"""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status

    @property
    def unavailable(self) -> bool:
        return self.status >= 500


@dataclass(frozen=True)
class Config:
    url: str
    sender_key: str
    target_key: str
    public_base_url: str
    # 这份配置对应的目标系统：提交、哈希与核销都按它。
    target: str = TARGET


@dataclass(frozen=True)
class Submitted:
    approval_id: str
    updated: bool


def web_url(settings: Settings) -> str | None:
    """审批系统给人看的网页入口（`TOWER_WEB_URL`）；没配、或配的不是 http(s) 地址时不给。

    给所有者的提醒与页面上的跳转只用它：`TOWER_URL` 是平台调审批系统接口的地址，不是给人
    打开的，不能拿来当链接。
    """
    url = (settings.tower_web_url or "").strip()
    return url if url.startswith(("https://", "http://")) else None


def config(settings: Settings, target: str = TARGET) -> Config | None:
    """审批闸门要用的部署配置；缺任何一项返回 None（提交失败、写操作不执行）。

    Args:
        target: 目标系统；它在 `TOWER_TARGET_KEYS` 里要有自己的 Key
    """
    key = settings.tower_target_keys.get(target)
    sender = settings.tower_sender_key
    if not (settings.tower_url and sender and key and settings.tower_callback_secret):
        return None
    if not (sender.get_secret_value() and key.get_secret_value()):
        return None
    return Config(
        settings.tower_url.rstrip("/"),
        sender.get_secret_value(),
        key.get_secret_value(),
        settings.public_base_url.rstrip("/"),
        target,
    )


def request_body(tool: str, calls: list[dict[str, Any]]) -> bytes:
    """暂存参数的规范化形式：键排序、无多余空白。提交与核销都从库里的参数重新算出它。

    文件以文件名、大小与 SHA-256 出现（`attachments.public`）：被批准的是这份内容，
    临时桶里的对象名不参与哈希。
    """
    return json.dumps(
        {"tool": tool, "calls": [attachments.public(call["arguments"]) for call in calls]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def batch_body(items: list[tuple[str, list[dict[str, Any]]]]) -> bytes:
    """合单的规范化请求体：各项（工具与参数）按单上的顺序。被批准的是整张单的这一份。"""
    return json.dumps(
        {
            "items": [
                {"tool": tool, "calls": [attachments.public(call["arguments"]) for call in calls]}
                for tool, calls in items
            ]
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def request_url(cfg: Config, external_id: str) -> str:
    """这张单在平台上的名义地址。平台不经 HTTP 调自己，它只用来算哈希、给 Tower 展示。"""
    return f"{cfg.public_base_url}/api/assistant/approvals/{external_id}/execute"


def request_hash(cfg: Config, external_id: str, body: bytes, caller: str) -> str:
    path = urllib.parse.urlsplit(request_url(cfg, external_id)).path
    return str(tower_gate.request_hash_parts("POST", cfg.target, path, "", body, caller))


async def _post(cfg: Config, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, timeout=TIMEOUT_SECONDS
        ) as http:
            resp = await http.post(
                cfg.url + path, json=payload, headers={"X-API-Key": cfg.sender_key}
            )
        data = resp.json() if resp.content else {}
    except (httpx.HTTPError, ValueError):
        raise TowerError("tower_unavailable", 503) from None
    if not isinstance(data, dict):
        raise TowerError("tower_unavailable", 503)
    return resp.status_code, data


def _refused(status: int, data: dict[str, Any]) -> TowerError:
    if status >= 500:
        return TowerError("tower_unavailable", 503)
    code = data.get("error")
    return TowerError(code if isinstance(code, str) and code else f"http_{status}", status)


async def submit(
    cfg: Config,
    *,
    external_id: str,
    requester: str,
    caller: str,
    abstract: str,
    content: str,
    body: bytes,
    expires_at: datetime,
) -> Submitted:
    """提交一张单；`external_id` 已有待批的单时 Tower 原地替换内容与请求、作废旧令牌并重新通知。

    审批人不填：Tower 让发起人本人确认。`expires_at` 是平台算的到期时刻（Tower 最长接受
    7 天），两边按同一个时刻过期。
    """
    status, data = await _post(
        cfg,
        "/v1/approvals",
        {
            "external_id": external_id,
            "approvers": [],
            "abstract": abstract,
            "content": content,
            "expires_at": expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "request": {
                "target": cfg.target,
                "caller": caller,
                "requester": requester,
                "method": "POST",
                "url": request_url(cfg, external_id),
                "body_b64": base64.b64encode(body).decode("ascii"),
                "summary": abstract,
            },
        },
    )
    approval_id = data.get("approval_id")
    if status not in (200, 201) or not isinstance(approval_id, str) or not approval_id:
        raise _refused(status, data)
    # 编号之后会拼进 URL 路径：只认 Tower 正常签发的字符。decisions 依赖 service，
    # service 依赖本模块，所以延迟导入。
    from coreman.core.cloud_proxy import decisions

    if not decisions.APPROVAL_ID.fullmatch(approval_id):
        raise TowerError("tower_unavailable", 503)
    return Submitted(approval_id, bool(data.get("updated")))


async def token(cfg: Config, approval_id: str) -> str:
    """批准后现取一次性令牌。"""
    status, data = await _post(cfg, f"/v1/approvals/{approval_id}/token", {})
    value = data.get("token")
    if status != 200 or not isinstance(value, str) or not value:
        raise _refused(status, data)
    return value


async def redeem(cfg: Config, token_value: str, req_hash: str, caller: str) -> str:
    """以目标系统的身份核销令牌，返回 Tower 的审批编号。"""
    try:
        found = await asyncio.to_thread(
            tower_gate.redeem, cfg.url, cfg.target_key, token_value, req_hash, caller
        )
    except tower_gate.TowerGateError as exc:
        raise TowerError(str(exc.code), int(exc.status)) from None
    return str(found)


async def report(cfg: Config, approval_id: str, status: str, error: str | None = None) -> None:
    """执行后上报 `executed` 或 `failed`；`error` 只放代码。"""
    payload: dict[str, Any] = {"status": status}
    if error:
        payload["error"] = error
    code, data = await _post(cfg, f"/v1/approvals/{approval_id}/result", payload)
    if code != 200:
        raise _refused(code, data)


async def withdraw(cfg: Config, external_id: str) -> None:
    """按外部编号撤回一张待批的单。已有结论时 Tower 返回 `already_final`；撤回之后不会回调。"""
    code, data = await _post(cfg, "/v1/approvals/withdraw", {"external_id": external_id})
    if code != 200:
        raise _refused(code, data)
