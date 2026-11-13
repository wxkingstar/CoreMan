"""助理设置的两个工具：所有者在私聊里说要改工作时段、时区、重点群、忽略的会话时用。

登记在助理工具注册表的 `knowledge` 组（不调飞书，授权换代后照常可用），但只在所有者发起的
轮次里受理：改设置等于改「看哪些群、什么时候看」，交回轮次、审批结果回合与子任务的输入可能
来自别人，不能让一条消息把某个群加进忽略名单。读也不受理：名单是加密列，无人在场时没有
理由拿出来。

校验在 `settings.update`（「我的助理」页用同一个函数）；这里的入参模型只挡类型与过长的值。
登记在注册表自己的文件末尾（`coreman/core/assistants/tools.py`），这里不导入它。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.records import is_assistant
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, input_schema
from coreman.core.timeutils import utcnow

Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
# 名单的上限由 settings.update 给出带数字的错误；这里只挡离谱的长度。
ChatList = Annotated[list[Short], Field(max_length=200)]


class Get(Arguments):
    pass


class Update(Arguments):
    timezone: Short | None = Field(default=None, description="IANA name such as Asia/Shanghai")
    work_start: Short | None = Field(
        default=None, description="HH:MM in the owner's time zone, e.g. 09:30"
    )
    work_end: Short | None = Field(
        default=None, description="HH:MM, later than work_start (no overnight shifts)"
    )
    workdays: list[int] | None = Field(
        default=None, max_length=7, description="ISO weekdays: 1 = Monday … 7 = Sunday"
    )
    focus_chats: ChatList | None = Field(
        default=None,
        description="The whole list of focus group chat ids (oc_…); replaces the current list."
        f" At most {assistant_settings.MAX_FOCUS}",
    )
    ignored_chats: ChatList | None = Field(
        default=None,
        description="The whole list of chat ids (oc_…) the owner does not want watched;"
        f" replaces the current list. At most {assistant_settings.MAX_IGNORED}",
    )


OWN: dict[str, tuple[type[Arguments], str]] = {
    "assistant_settings_get": (
        Get,
        "Read the owner's assistant settings: time zone, working hours and days, focus chats,"
        " ignored chats, whether the assistant is on shift now and any temporary switch.",
    ),
    "assistant_settings_update": (
        Update,
        "Change the owner's assistant settings; only what you pass changes. Chat lists are"
        " replaced whole: read the current list first, then pass the complete new list."
        " Only when the owner asks for it in this private chat.",
    ),
}
OWNER_HINT = (
    "Settings are read and changed only in a turn the owner started in the private chat."
    " Nobody is present in this turn: leave them as they are and tell the owner what you"
    " would change."
)
DEFINITIONS: list[dict[str, Any]] = [
    {"name": name, "description": text, "inputSchema": input_schema(args)}
    for name, (args, text) in OWN.items()
]


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """执行一个设置工具；调用方（MCP 入口）负责提交。`cipher` 用不上：名单由数据密钥加解密。"""
    found = OWN.get(name)
    if found is None or not is_assistant(scope.bot):
        return {"error": "invalid_tool_or_arguments"}
    if scope.unattended:
        return {"error": "owner_turn_required", "hint": OWNER_HINT}
    try:
        args: Any = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {
            "error": "invalid_tool_or_arguments",
            "invalid": approvals_service._problems(exc),
        }
    now, bot_id = utcnow(), scope.bot.id
    try:
        if name == "assistant_settings_update":
            row = await assistant_settings.update(
                session, bot_id, now=now, **args.model_dump(exclude_none=True)
            )
            return {"status": "saved", "settings": assistant_settings.view(row, now)}
        row = await assistant_settings.get(session, bot_id)
        return {"settings": assistant_settings.view(row, now)}
    except assistant_settings.SettingsError as exc:
        return exc.payload()
