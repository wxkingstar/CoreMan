"""chat 任务处理器的兼容入口：实现按流水线阶段拆在 coreman.runtime.worker.chat 子包里。

保留这个模块名，已有的 import（worker 入口、提交轮处理器、测试）不必跟着改。
"""

from coreman.core.bus import tasks
from coreman.runtime.worker.chat import (
    ChatTaskHandler,
    Intake,
    Outcome,
    Prepared,
    Verdict,
    joined_text,
    log_entry,
    message_type_of,
    next_event,
    part_kind,
    session_viewer_url,
    strip_mention,
)

# tasks 一并导出：旧代码与测试经 chat_handler.tasks 取总线的 tasks 模块。
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
    "tasks",
]
