"""对话域：发言者身份解析、relay 会话映射、对话日志落库（spec §8.2、§8.5、§5.7）。"""

from coreman.core.chat.chat_logs import LIMITS, ChatLogEntry, ChatLogWriter
from coreman.core.chat.identity import looks_like_open_userid, resolve_speaker
from coreman.core.chat.sessions import SessionInfo

__all__ = [
    "LIMITS",
    "ChatLogEntry",
    "ChatLogWriter",
    "SessionInfo",
    "looks_like_open_userid",
    "resolve_speaker",
]
