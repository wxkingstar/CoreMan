"""口吻样本的保存工具：只在冷启动那一轮（`assistant_cold_start`）里有，也只有它。

登记在助理工具注册表的 `voice` 组。入参只有正文：写哪一页、是否置顶都由平台定
（`voice.save`）。分派时再核对一次任务类型，注册表的分组之外多一道。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import notes, voice
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.kinds import COLD_START_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, input_schema


class Save(Arguments):
    body: Annotated[str, StringConstraints(min_length=1, max_length=voice.MAX_BODY_BYTES)] = Field(
        description="The whole voice page in Markdown"
    )


OWN: dict[str, tuple[type[Arguments], str]] = {
    "assistant_voice_save": (
        Save,
        "Save the owner's voice page (how the owner writes). It becomes the pinned page"
        " profile/voice.md. The only tool in this turn.",
    ),
}
DEFINITIONS: list[dict[str, Any]] = [
    {"name": name, "description": text, "inputSchema": input_schema(args)}
    for name, (args, text) in OWN.items()
]


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """保存口吻页；调用方（MCP 入口）负责提交。别的任务类型的轮次一律不受理。"""
    found = OWN.get(name)
    if (
        found is None
        or not is_assistant(scope.bot)
        or getattr(scope.task, "kind", None) != COLD_START_KIND
    ):
        return {"error": "invalid_tool_or_arguments"}
    try:
        args: Any = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {
            "error": "invalid_tool_or_arguments",
            "invalid": approvals_service._problems(exc),
        }
    try:
        entry = await voice.save(session, scope.bot.id, args.body)
    except notes.NotesError as exc:
        return exc.payload()
    if entry is None:
        return {
            "status": "kept",
            "path": voice.PATH,
            "hint": "所有者自己写过这一页，保留他的版本，不用再保存。",
        }
    return {"status": "saved", "path": entry.path, "version": entry.version, "pinned": entry.pinned}
