"""云服务代理的本轮能力令牌：哪一轮、哪个员工、谁在说话。

仿个人凭证的令牌，AAD 独立：拿业务系统或个人凭证的令牌读不通这里。令牌只证明
「这一轮是谁」，能调哪些产品与动作、要不要审批由代理每次调用时重新判断。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from coreman.core.crypto import Cipher

CAPABILITY_AAD = "cloud_proxy.capability.v1"
ENV_PREFIX = "COREMAN_CLOUD_"
URL_ENV = ENV_PREFIX + "PROXY_URL"
TOKEN_ENV = ENV_PREFIX + "PROXY_TOKEN"
APPROVERS_ENV = "COREMAN_APPROVERS"
# agent 在这个地址后面补 /aliyun/{product}，给以后接别的云厂商留出路径。
API_PATH = "/api/runtime/cloud"
# 比 SSE 超时多留的时间：一轮跑到最后一刻发出的调用也不能因令牌刚好过期失败。
CAPABILITY_GRACE = 300


@dataclass(frozen=True)
class Capability:
    task_id: int
    bot_id: uuid.UUID
    user_id: uuid.UUID
    origin_kind: str
    session_key: str | None
    # 本轮用的 relay 会话（定时任务没有）。
    relay_session_id: uuid.UUID | None
    event_id: int | None
    cron_job_id: uuid.UUID | None


def issue_capability(cipher: Cipher, cap: Capability, *, ttl_seconds: int) -> str:
    claims = {
        "task": cap.task_id,
        "bot": str(cap.bot_id),
        "user": str(cap.user_id),
        "origin": cap.origin_kind,
        "session_key": cap.session_key,
        "relay": str(cap.relay_session_id) if cap.relay_session_id else None,
        "event": cap.event_id,
        "cron": str(cap.cron_job_id) if cap.cron_job_id else None,
        "exp": time.time() + ttl_seconds,
    }
    return cipher.encrypt(json.dumps(claims), CAPABILITY_AAD)


def read_capability(cipher: Cipher, token: str) -> Capability:
    data = json.loads(cipher.decrypt(token, CAPABILITY_AAD))
    if not isinstance(data, dict) or float(data["exp"]) <= time.time():
        raise ValueError("expired_capability")
    return Capability(
        task_id=int(data["task"]),
        bot_id=uuid.UUID(data["bot"]),
        user_id=uuid.UUID(data["user"]),
        origin_kind=str(data["origin"]),
        session_key=data.get("session_key"),
        relay_session_id=uuid.UUID(data["relay"]) if data.get("relay") else None,
        event_id=int(data["event"]) if data.get("event") is not None else None,
        cron_job_id=uuid.UUID(data["cron"]) if data.get("cron") else None,
    )


def apply_env(
    env: dict[str, str],
    cipher: Cipher,
    cap: Capability,
    *,
    public_base_url: str,
    sse_timeout_seconds: int,
) -> dict[str, str]:
    """丢掉任何已有的 COREMAN_CLOUD_* 再写入本轮的地址与令牌。"""
    out = {k: v for k, v in env.items() if not k.startswith(ENV_PREFIX)}
    out[URL_ENV] = public_base_url.rstrip("/") + API_PATH
    out[TOKEN_ENV] = issue_capability(
        cipher, cap, ttl_seconds=sse_timeout_seconds + CAPABILITY_GRACE
    )
    return out
