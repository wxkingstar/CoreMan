"""structlog JSON 日志：字段 ts/level/service/instance/event，凭证脱敏。"""

from __future__ import annotations

import logging
import re
import sys
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
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
