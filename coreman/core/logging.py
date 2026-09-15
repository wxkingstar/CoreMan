"""structlog JSON 日志：字段 ts/level/service/instance/event，凭证脱敏。"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog
from structlog.types import EventDict, Processor

_SECRET_MARKERS = ("secret", "token", "password", "authorization", "api_key", "master_key")


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
    logging.basicConfig(
        level=numeric, stream=stream or sys.stdout, format="%(message)s", force=True
    )
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
