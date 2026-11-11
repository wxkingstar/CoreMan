"""structlog JSON 日志：字段 ts/level/service/instance/event，凭证脱敏。"""

from __future__ import annotations

import copy
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from typing import Any, TextIO

import structlog
from structlog.types import EventDict, Processor

_SECRET_MARKERS = ("secret", "token", "password", "authorization", "api_key", "master_key")

# httpx 每发一个请求就打一行带完整 URL 的 INFO，而企业微信把 access_token、corpsecret、OAuth code
# 放在查询串里，这行会把在用的凭证写进容器日志。钉在 WARNING：进程开 LOG_LEVEL=DEBUG 也不放出来。
QUIET_LOGGERS = ("httpx", "httpcore")

# 兜底：异常文本（HTTPStatusError 会带上请求 URL）、第三方库日志里的 URL，查询串凭证值一律换成 ***。
_URL_CREDENTIAL = re.compile(
    r"([?&](?:[\w.-]*(?:token|secret|password|ticket)|(?:[\w.-]*_)?code|(?:api_?|access_?)?key)=)"
    r"[^&#\s\"'\\]+",
    re.IGNORECASE,
)


def redact_url_credentials(text: str) -> str:
    return _URL_CREDENTIAL.sub(r"\1***", text)


class RedactingFormatter(logging.Formatter):
    """格式化完再脱敏，traceback 也在内；inner 给出时沿用它的格式（包 uvicorn 自带的格式化器）。"""

    def __init__(self, fmt: str | None = None, *, inner: logging.Formatter | None = None) -> None:
        super().__init__(fmt)
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info and _private_content.get():
            # `log.exception` 还会让标准库在消息后面再附一份 traceback：处理个人助理的内容时
            # 不附（JSON 行里已有类型与调用栈，见 strip_exception_text）。
            record = copy.copy(record)
            record.exc_info, record.exc_text = None, None
        text = self._inner.format(record) if self._inner else super().format(record)
        return redact_url_credentials(text)


def _redact_existing_handlers() -> None:
    """uvicorn 在 lifespan 之前就给自己的 logger 装好了处理器（访问日志、
    "Exception in ASGI application" 的 traceback），这些记录不经根处理器，要单独包一层。"""
    for logger in list(logging.root.manager.loggerDict.values()):
        if not isinstance(logger, logging.Logger):
            continue
        for handler in logger.handlers:
            if not isinstance(handler.formatter, RedactingFormatter):
                handler.setFormatter(RedactingFormatter(inner=handler.formatter))


def scrub_secrets(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        lowered = key.lower()
        if key != "event" and any(marker in lowered for marker in _SECRET_MARKERS):
            event_dict[key] = "***"
    return event_dict


# 当前这段处理（一个任务、网关的一轮推送）经手的是个人助理的内容：异常文字可能夹带对话片段
# （校验错误回显输入值、取值失败带出原值），日志里只留异常类型与调用栈。
_private_content: ContextVar[bool] = ContextVar("coreman_private_content", default=False)


def mark_private_content(on: bool = True) -> None:
    """标记（或取消）当前上下文正在处理个人助理的内容；随 asyncio 任务与 `to_thread` 继承。"""
    _private_content.set(on)


def private_content() -> bool:
    return _private_content.get()


def strip_exception_text(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """处理个人助理的内容时，`exc_info` 换成「类型 + 调用栈」，不带异常消息。"""
    exc_info = event_dict.get("exc_info")
    if not exc_info or not _private_content.get():
        return event_dict
    if isinstance(exc_info, BaseException):
        exc: BaseException | None = exc_info
    elif isinstance(exc_info, tuple):
        exc = exc_info[1]
    else:
        exc = sys.exc_info()[1]
    del event_dict["exc_info"]
    if exc is not None:
        frames = "".join(traceback.format_tb(exc.__traceback__))
        event_dict["exception"] = f"{type(exc).__name__}\n{frames}".rstrip()
    return event_dict


def _static_fields(service: str, instance: str) -> Processor:
    """把 service/instance 做成处理器闭包，而不是 bind_contextvars。

    contextvars 只沿任务创建链继承：configure_logging 在 lifespan 任务里执行，uvicorn 为
    每个请求另起任务，绑定的值对请求路径的日志不可见（bootstrap_login_failed、
    unhandled_error 等会缺字段）。闭包对任何上下文都生效。
    """

    def processor(_: Any, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("instance", instance)
        return event_dict

    return processor


def configure_logging(
    service: str, instance: str, level: str = "INFO", stream: TextIO | None = None
) -> None:
    """默认写 stdout；stream 显式给出时写到它（CLI 用 stderr，别和结果 JSON 混在一起）。

    默认值取 None 而不是 sys.stdout：后者会在导入时固化当时的 sys.stdout，
    pytest 的 capsys 之类在运行期替换 sys.stdout 的场景就再也捕获不到日志了。
    """
    numeric = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logging.basicConfig(level=numeric, handlers=[handler], force=True)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _redact_existing_handlers()
    structlog.configure(
        processors=[
            _static_fields(service, instance),
            # 保留：在请求入口绑定 task_id / stream_id 等请求级字段。
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            scrub_secrets,  # type: ignore[list-item]
            strip_exception_text,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
