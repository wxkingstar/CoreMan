"""chat 任务处理器子包：按流水线阶段拆分，ChatTaskHandler 在 handler 模块里组合各阶段。"""

from coreman.runtime.worker.chat.converse import next_event
from coreman.runtime.worker.chat.handler import ChatTaskHandler
from coreman.runtime.worker.chat.inbound import (
    joined_text,
    message_type_of,
    part_kind,
    strip_mention,
)
from coreman.runtime.worker.chat.models import Intake, Outcome, Prepared, Verdict
from coreman.runtime.worker.chat.opening import session_viewer_url
from coreman.runtime.worker.chat.records import log_entry

__all__ = [
    "ChatTaskHandler",
    "Intake",
    "Prepared",
    "Outcome",
    "Verdict",
    "part_kind",
    "message_type_of",
    "joined_text",
    "strip_mention",
    "log_entry",
    "next_event",
    "session_viewer_url",
]
