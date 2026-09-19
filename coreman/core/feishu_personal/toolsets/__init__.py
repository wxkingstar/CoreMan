"""Typed personal tools by product area; each one calls only registered endpoints."""

from __future__ import annotations

from coreman.core.feishu_personal.toolbase import Tool
from coreman.core.feishu_personal.toolsets import (
    calendar,
    docs,
    files,
    mail,
    meetings,
    messages,
    org,
    tasks,
)

ALL: list[Tool] = [
    *messages.TOOLS,
    *meetings.TOOLS,
    *calendar.TOOLS,
    *mail.TOOLS,
    *tasks.TOOLS,
    *docs.TOOLS,
    *files.TOOLS,
    *org.TOOLS,
]
