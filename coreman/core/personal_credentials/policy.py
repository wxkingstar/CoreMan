"""个人凭证的校验规则、加密 AAD 与本轮索取令牌。

索取哪些凭证由 agent 按场景决定，这里只保留技术上必须的约束：键名能当环境变量用、
不覆盖平台保留与控制类变量；值是单行、有长度上限。agent 同时决定值要不要保存
（`save`）：本人以后还要用的账号密钥才保存，交给它写进服务器或配置的一次性交付不保存。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coreman.core.bots.env_policy import is_blocked_env_key
from coreman.core.bots.secrets import ENV_KEY_RE
from coreman.core.crypto import Cipher
from coreman.core.prompting.env_vars import is_reserved_key

ENV_PREFIX = "COREMAN_CREDENTIAL_"
CARD_PREFIX = "credential@"
CAPABILITY_AAD = "personal_credentials.capability.v1"
CAPABILITY_GRACE = 300
REQUEST_TTL = timedelta(hours=1)
# 一次性交付的值最长留多久：正常在续接轮结束时就擦掉，这是续接任务迟迟没跑完时的兜底。
HANDOFF_MAX_AGE = timedelta(hours=2)
MAX_FIELDS = 20
MAX_VALUE = 4096
FEISHU_MAX_VALUE = 1000
MIN_REDACT = 6
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class CredentialError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def value_aad(bot_id: uuid.UUID | str, user_id: uuid.UUID | str, env_key: str) -> str:
    return f"personal_credentials.value_enc:{bot_id}:{user_id}:{env_key}"


def sealed_aad(request_id: uuid.UUID | str) -> str:
    return f"credential_requests.sealed:{request_id}"


def handoff_aad(request_id: uuid.UUID | str) -> str:
    return f"credential_requests.handoff:{request_id}"


def key_problem(key: str) -> str | None:
    if not ENV_KEY_RE.fullmatch(key):
        return "变量名只能用大写字母、数字和下划线，以字母开头，最长 64 位"
    if key.startswith("COREMAN_"):
        return "COREMAN_ 开头的变量由平台保留"
    if is_reserved_key(key):
        return "这是平台按发言者生成的保留变量"
    if is_blocked_env_key(key):
        return "这个变量会改变运行时行为，不允许设置"
    return None


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=64)
    label: str = Field(min_length=1, max_length=50)
    secret: bool = True
    placeholder: str = Field(default="", max_length=100)


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[FieldSpec] = Field(min_length=1, max_length=MAX_FIELDS)
    purpose: str = Field(min_length=1, max_length=300)
    # 不填按一次性：判断漏了最多让用户再填一次，不会把服务器密钥当个人凭证长期留着。
    save: bool = False


def parse_request(body: Any) -> RequestBody:
    try:
        parsed = RequestBody.model_validate(body)
    except ValidationError as exc:
        names = sorted({".".join(str(p) for p in err["loc"]) or "body" for err in exc.errors()})
        raise CredentialError("invalid_fields", "参数无效：" + "、".join(names)) from None
    problems = [f"{f.key}：{p}" for f in parsed.fields if (p := key_problem(f.key))]
    keys = [f.key for f in parsed.fields]
    if len(set(keys)) != len(keys):
        problems.append("同一请求里的变量名不能重复")
    if problems:
        raise CredentialError("invalid_fields", "；".join(problems))
    return parsed


def clean_values(
    fields: list[dict[str, Any]], values: Any, *, limit: int = MAX_VALUE
) -> dict[str, str]:
    """按请求的字段取值：每个都必填、去首尾空白、单行、长度 1–limit；多出来的键忽略。"""
    if not isinstance(values, dict):
        raise CredentialError("invalid_values", "提交内容格式不对")
    out: dict[str, str] = {}
    for field in fields:
        key = str(field["key"])
        label = str(field.get("label") or key)
        raw = values.get(key)
        value = raw.strip() if isinstance(raw, str) else ""
        if not value:
            raise CredentialError("invalid_values", f"「{label}」不能为空")
        if len(value) > limit:
            raise CredentialError("invalid_values", f"「{label}」超过 {limit} 个字符")
        if _CONTROL.search(value):
            raise CredentialError("invalid_values", f"「{label}」不能包含换行或控制字符")
        out[key] = value
    return out


def card_task_id(request_id: uuid.UUID | str) -> str:
    return f"{CARD_PREFIX}{request_id}"


def parse_card_task_id(task_id: str) -> uuid.UUID | None:
    if not task_id.startswith(CARD_PREFIX):
        return None
    try:
        return uuid.UUID(task_id[len(CARD_PREFIX) :])
    except ValueError:
        return None


@dataclass(frozen=True)
class Capability:
    """本轮令牌指向的一切：哪一轮、哪个员工、谁的凭证、表单发回哪里。"""

    task_id: int
    bot_id: uuid.UUID
    user_id: uuid.UUID
    origin_kind: str
    chat_id: str
    chat_type: str
    session_key: str | None
    # 本轮用的 relay 会话（定时任务没有）：续接前凭它确认对话没有被重置或切走。
    relay_session_id: uuid.UUID | None
    event_id: int | None
    cron_job_id: uuid.UUID | None


def issue_capability(cipher: Cipher, cap: Capability, *, ttl_seconds: int) -> str:
    claims = {
        "task": cap.task_id,
        "bot": str(cap.bot_id),
        "user": str(cap.user_id),
        "origin": cap.origin_kind,
        "chat": cap.chat_id,
        "chat_type": cap.chat_type,
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
        chat_id=str(data["chat"]),
        chat_type=str(data["chat_type"]),
        session_key=data.get("session_key"),
        relay_session_id=uuid.UUID(data["relay"]) if data.get("relay") else None,
        event_id=int(data["event"]) if data.get("event") is not None else None,
        cron_job_id=uuid.UUID(data["cron"]) if data.get("cron") else None,
    )
