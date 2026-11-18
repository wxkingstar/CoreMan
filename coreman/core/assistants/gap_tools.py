"""能力缺口工具：助理登记办不成事时卡在哪个权限上（`gap_report`），看已登记的（`gap_list`），
为白名单外的业务系统提交权限申请（`gap_request`，只在所有者在场、当面同意理由的一轮里）。

登记在助理工具注册表的 `knowledge` 组：数据在平台自己的库里，子任务也能用。分派只认这一轮核验
过的助理记录（`scope.bot`）。登记在注册表自己的文件末尾（`tools.py`），这里不导入它。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import access_requests, capabilities, followups, gaps
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.followup_tools import Label
from coreman.core.assistants.records import is_assistant
from coreman.core.crypto import Cipher
from coreman.core.db.models import GAP_STATUSES, User
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


class Request(Arguments):
    id: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[Gg]?\d{1,9}$")]
    reason: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=access_requests.MAX_REASON
        ),
        Field(
            description="The reason the owner agreed to in this chat; administrators will read it"
        ),
    ]
    write: bool = False


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
    "gap_request": (
        Request,
        "Submit an access request for a business system gap that is not open to you. Only in a turn"
        " the owner started, and only after he agreed to the reason in this chat; platform"
        " administrators decide.",
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
REQUEST_HINTS = {
    "owner_turn_required": (
        "申请只能在所有者本人在场的一轮里、他当面同意理由后提交；记进待跟进，等他在场时再办。"
    ),
    "owner_can_grant": (
        "这个系统对你开放，所有者自己就能开：请他在「我的助理」页这一条上点「开通」。"
    ),
    "already_pending": "已经有一张在途的申请，等平台管理员处理。",
    "gap_not_open": "这个缺口不是待开通的状态（已申请、已解决或所有者不开）。",
    "gap_not_found": "没有这个缺口；用 gap_list 看现有的。",
    "not_business_system": "只有业务系统的缺口能申请。",
    "system_not_requestable": "这个系统没有登记或已停用，请所有者联系平台管理员。",
    "invalid_reason": f"reason 写一两句话，不超过 {access_requests.MAX_REASON} 字。",
    "requested": "已提交，平台管理员处理后会交回给你；告诉所有者已经提交了。",
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
    if name == "gap_request":
        return await _request(session, scope, args)
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


async def _request(session: AsyncSession, scope: Scope, args: Request) -> dict[str, Any]:
    """所有者在场、当面同意理由后提交申请；平台来源与审批结果的轮次一律不受理。

    `scope` 是这一轮核验过的所有者与他的助理（`scope.user_id` 就是所有者），不另收身份参数。
    """
    if scope.unattended:
        return {"error": "owner_turn_required", "hint": REQUEST_HINTS["owner_turn_required"]}
    owner = await session.get(User, scope.user_id)
    if owner is None or scope.bot.owner_id != owner.id or not scope.bot.enabled:
        return {"error": "owner_turn_required", "hint": REQUEST_HINTS["owner_turn_required"]}
    try:
        row = await access_requests.open_request(
            session,
            scope.bot,
            gaps.parse_label(args.id),
            write=args.write,
            reason=args.reason,
            actor=owner,
        )
    except (gaps.GapError, access_requests.RequestError) as exc:
        return {"error": exc.code, "hint": REQUEST_HINTS.get(exc.code, "")}
    return {"status": "requested", "request_id": str(row.id), "hint": REQUEST_HINTS["requested"]}
