"""通讯录、审批、OKR、考勤：都以本人身份，只看本人能看的。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import StringConstraints, model_validator

from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Day,
    Identifier,
    Page,
    compact,
    page,
    tool,
    valid_days,
)

Keyword = Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]
Comment = Annotated[str, StringConstraints(max_length=2000)]
_TOPICS = {"todo": "1", "done": "2", "cc_unread": "17", "cc_read": "18"}


class SearchUsers(Page):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class UserRef(Arguments):
    open_id: Identifier


class ApprovalTasks(Page):
    topic: Literal["todo", "done", "cc_unread", "cc_read"] = "todo"
    keyword: Keyword = ""


class Initiated(Page):
    keyword: Keyword = ""


class Instance(Arguments):
    instance_code: Identifier


class Decision(Instance):
    task_id: Identifier
    comment: Comment | None = None


class Objectives(Page):
    cycle_id: Identifier


class KeyResults(Page):
    objective_id: Identifier


class Attendance(Arguments):
    start_date: Day
    end_date: Day

    _days = valid_days("start_date", "end_date")

    @model_validator(mode="after")
    def short_range(self) -> Attendance:
        days = (date.fromisoformat(self.end_date) - date.fromisoformat(self.start_date)).days
        if not 0 <= days <= 31:
            raise ValueError("end_date must be within 31 days after start_date")
        return self


@tool(
    "feishu_search_users",
    SearchUsers,
    "contact.search",
    "Find colleagues by name, pinyin or email to get their open_id.",
)
async def search_users(args: SearchUsers, call: Call) -> dict[str, Any]:
    return await call(
        "contact.search",
        params=compact({"page_size": args.limit, "page_token": args.page_token}),
        json={"query": args.query},
    )


@tool("feishu_get_user", UserRef, "contact.user", "Read a colleague's profile by open_id.")
async def get_user(args: UserRef, call: Call) -> dict[str, Any]:
    return await call(
        "contact.user", path={"user_id": args.open_id}, params={"user_id_type": "open_id"}
    )


@tool(
    "feishu_approval_tasks",
    ApprovalTasks,
    "approval.tasks",
    "List approvals waiting for the user, handled by them, or copied to them.",
)
async def approval_tasks(args: ApprovalTasks, call: Call) -> dict[str, Any]:
    params = {**page(args), "topic": _TOPICS[args.topic], "user_id_type": "open_id"}
    return await call("approval.tasks", params=compact({**params, "keyword": args.keyword or None}))


@tool(
    "feishu_approval_initiated",
    Initiated,
    "approval.initiated",
    "List approvals the user submitted.",
)
async def approval_initiated(args: Initiated, call: Call) -> dict[str, Any]:
    params = {**page(args), "user_id_type": "open_id", "keyword": args.keyword or None}
    return await call("approval.initiated", params=compact(params))


@tool(
    "feishu_approval_instance",
    Instance,
    "approval.instance",
    "Read an approval's form, progress and the user's task IDs.",
)
async def approval_instance(args: Instance, call: Call) -> dict[str, Any]:
    return await call(
        "approval.instance",
        params={"instance_code": args.instance_code, "user_id_type": "open_id"},
    )


@tool(
    "feishu_approval_approve",
    Decision,
    "approval.approve",
    "Approve the user's pending approval task.",
)
async def approve(args: Decision, call: Call) -> dict[str, Any]:
    return await call("approval.approve", json=args.model_dump(exclude_none=True))


@tool(
    "feishu_approval_reject",
    Decision,
    "approval.reject",
    "Reject the user's pending approval task.",
)
async def reject(args: Decision, call: Call) -> dict[str, Any]:
    return await call("approval.reject", json=args.model_dump(exclude_none=True))


@tool("feishu_okr_cycles", Page, "okr.cycles", "List the user's OKR cycles.")
async def okr_cycles(args: Page, call: Call) -> dict[str, Any]:
    return await call(
        "okr.cycles",
        params={**page(args), "user_id": call.scope.open_id, "user_id_type": "open_id"},
    )


@tool("feishu_okr_objectives", Objectives, "okr.objectives", "List objectives in an OKR cycle.")
async def okr_objectives(args: Objectives, call: Call) -> dict[str, Any]:
    return await call(
        "okr.objectives",
        path={"cycle_id": args.cycle_id},
        params={**page(args), "user_id_type": "open_id"},
    )


@tool("feishu_okr_key_results", KeyResults, "okr.key_results", "List an objective's key results.")
async def okr_key_results(args: KeyResults, call: Call) -> dict[str, Any]:
    return await call(
        "okr.key_results",
        path={"objective_id": args.objective_id},
        params={**page(args), "user_id_type": "open_id"},
    )


@tool(
    "feishu_attendance",
    Attendance,
    "attendance.results",
    "The user's own clock-in results per day, for up to 31 days.",
)
async def attendance(args: Attendance, call: Call) -> dict[str, Any]:
    return await call(
        "attendance.results",
        params={"employee_type": "employee_id"},
        json={
            "user_ids": [call.scope.platform_user_id],
            "check_date_from": int(args.start_date.replace("-", "")),
            "check_date_to": int(args.end_date.replace("-", "")),
        },
    )


TOOLS = [
    search_users,
    get_user,
    approval_tasks,
    approval_initiated,
    approval_instance,
    approve,
    reject,
    okr_cycles,
    okr_objectives,
    okr_key_results,
    attendance,
]
