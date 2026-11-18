"""能力缺口工具：助理登记办不成事时卡在哪个权限上（`gap_report`），看已登记的（`gap_list`）。

登记在助理工具注册表的 `knowledge` 组：数据在平台自己的库里，子任务也能用。分派只认这一轮核验
过的助理记录（`scope.bot`）。登记在注册表自己的文件末尾（`tools.py`），这里不导入它。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import capabilities, followups, gaps
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.followup_tools import Label
from coreman.core.assistants.records import is_assistant
from coreman.core.crypto import Cipher
from coreman.core.db.models import GAP_STATUSES
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, input_schema

Kind = Literal[
    "feishu_scope", "business_system", "cloud_product", "credential", "node_tool", "other"
]


class Report(Arguments):
    kind: Kind
    key: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        Field(
            description=(
                "A code name, not prose: Feishu scope or need (im:message, send_messages), business"
                " system key, cloud product (alidns), credential env name (GIT_TOKEN), CLI name,"
                " or a short English slug for other"
            )
        ),
    ]
    need: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=gaps.MAX_NEED),
        Field(description="One sentence: what you need it for"),
    ]
    followups: list[Label] = Field(
        default_factory=list, max_length=20, description="Follow-ups it blocks, such as F012"
    )
    resolved: bool = Field(
        default=False, description="node_tool / other only: you checked and it works now"
    )


class Listing(Arguments):
    status: Literal["live", "declined", "all"] = "live"


OWN: dict[str, tuple[type[Arguments], str]] = {
    "gap_report": (
        Report,
        "Record a capability you lack (a permission, system, product, credential or tool) and the"
        " follow-ups it blocks. Same kind+key merges into one gap. The owner sees it on his board"
        " with how to enable it; when it becomes usable the platform hands the blocked follow-ups"
        " back to you.",
    ),
    "gap_list": (
        Listing,
        "List your capability gaps: live (open or requested), declined (owner said no), or all.",
    ),
}
STATUSES = {"live": gaps.LIVE, "declined": ("declined",), "all": GAP_STATUSES}
DECLINED_HINT = (
    "所有者已经明确不开这个权限：不要再为它登记或提醒他，换别的办法，或在待跟进里写明办不成。"
)
HINTS = {
    "invalid_key": (
        "key 写代号：飞书权限名或档位、业务系统标识、云产品名、凭证的环境变量名、命令名，"
        "其他用英文短名；只能有字母、数字和 _ . : / -。"
    ),
    "invalid_need": f"need 写一句话，不超过 {gaps.MAX_NEED} 字。",
    "resolve_not_allowed": (
        "resolved 只能用于 node_tool 与 other；其余种类开通后平台会自动记为已解决。"
    ),
    "gap_not_found": "没有登记过这个缺口，不用报已解决。",
    "too_many_gaps": f"未解决的缺口已到 {gaps.MAX_LIVE} 个上限，先把能用的报成已解决。",
    "followup_not_found": "followups 里有不存在的编号；用 followup_list 看现有的。",
    "followup_closed": "followups 里有已关闭的条目，不用再挂。",
    "content_locked": "所有者的数据密钥不可用，现在登记不了；不要重试，如实告诉所有者。",
}


def definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": text, "inputSchema": input_schema(args)}
        for name, (args, text) in OWN.items()
    ]


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """执行一个缺口工具；调用方（MCP 入口）负责提交。`cipher` 用不上。"""
    found = OWN.get(name)
    if found is None or not is_assistant(scope.bot):
        return {"error": "invalid_tool_or_arguments"}
    try:
        args: Any = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {"error": "invalid_tool_or_arguments", "invalid": approvals_service._problems(exc)}
    bot_id = scope.bot.id
    try:
        if name == "gap_list":
            rows = await gaps.listing(session, bot_id, STATUSES[args.status])
            return {
                "gaps": [
                    gaps.view(row, await gaps.fix_for(session, bot_id, row), seqs)
                    for row, seqs in rows
                ]
            }
        seqs = [followups.parse_label(value) for value in args.followups]
        # 报的时候这项能力已有的级别由平台按授权算，不由助理填：之后升上去才算开通。
        rank = await capabilities.current_rank(session, bot_id, args.kind, args.key)
        row, outcome = await gaps.report(
            session,
            bot_id,
            kind=args.kind,
            key=args.key,
            need=args.need,
            followup_seqs=seqs,
            resolved=args.resolved,
            seen_rank=rank,
        )
    except (gaps.GapError, followups.FollowupError) as exc:
        out: dict[str, Any] = {"error": exc.code}
        if exc.code in HINTS:
            out["hint"] = HINTS[exc.code]
        return out
    result: dict[str, Any] = {"gap": await gaps.view_of(session, bot_id, row), "outcome": outcome}
    if outcome == "declined":
        result["hint"] = DECLINED_HINT
    return result
