"""`assistant_report`：无人在场的轮次里记下要告诉所有者的话（待汇报），由平台合并成摘要发出。

登记在助理工具注册表的 `approval` 组（只有主会话的轮次有，子任务与冷启动没有）。所有者在场的
轮次调用返回 `owner_present`：那时直接回复他。记录与发送见 `digest.py`。

登记在注册表自己的文件末尾（`coreman/core/assistants/tools.py`），这里不导入它。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import digest
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.records import is_assistant
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, input_schema


class Report(Arguments):
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=digest.MAX_REPORT)
    ] = Field(description="One or two sentences the owner needs to know, in his language")
    urgent: bool = Field(
        default=False,
        description="true only when it cannot wait for the next digest: it has a deadline"
        " before then, or the owner himself must decide right now",
    )


NAME = "assistant_report"
DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": NAME,
        "description": (
            "In a turn the owner is not present in (handover, job result, approval result):"
            " record something the owner needs to know. Nothing is sent now: the platform"
            " merges these into a digest at the owner's digest times; urgent ones go out when"
            " this turn ends (never during his quiet hours). Not calling it means there is"
            " nothing to tell him. Your reply text in such a turn reaches nobody."
        ),
        "inputSchema": input_schema(Report),
    }
]


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """调用方（MCP 入口）负责提交。`cipher` 用不上：正文由数据密钥加密。"""
    if name != NAME or not is_assistant(scope.bot):
        return {"error": "invalid_tool_or_arguments"}
    try:
        args = Report.model_validate(arguments)
    except ValidationError as exc:
        return {"error": "invalid_tool_or_arguments", "invalid": approvals_service._problems(exc)}
    return await digest.record(session, scope, args.text, urgent=args.urgent)
