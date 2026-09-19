"""通讯录、审批（查看、处理、发起）、OKR、考勤：都以本人身份，只看本人能看的。"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Day,
    Identifier,
    OpenIds,
    Page,
    Uuid,
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


class SearchDepartments(Page):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class DepartmentRef(Arguments):
    department_id: Identifier = Field(description="open_department_id")


class DepartmentMembers(Page, DepartmentRef):
    pass


class ProgressFields(Arguments):
    text: Annotated[str, StringConstraints(min_length=1, max_length=5000)]
    percent: float | None = Field(default=None, ge=0, le=100, description="Progress in %")
    status: Literal["normal", "overdue", "done"] | None = None

    def body(self) -> dict[str, Any]:
        content = {
            "blocks": [
                {
                    "type": "paragraph",
                    "paragraph": {
                        "elements": [{"type": "textRun", "textRun": {"text": self.text}}]
                    },
                }
            ]
        }
        body: dict[str, Any] = {"content": content}
        if self.percent is not None:
            rate: dict[str, Any] = {"percent": self.percent}
            if self.status is not None:
                rate["status"] = {"normal": 0, "overdue": 1, "done": 2}[self.status]
            body["progress_rate"] = rate
        return body


class AddProgress(ProgressFields):
    target_type: Literal["objective", "key_result"]
    target_id: Identifier


class UpdateProgress(ProgressFields):
    progress_id: Identifier


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


class Templates(Page):
    keyword: Keyword = ""


class Template(Arguments):
    approval_code: Identifier


class FormValue(Arguments):
    id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    type: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]{1,50}$")]
    value: JsonValue


class NodeUsers(Arguments):
    key: Annotated[str, StringConstraints(min_length=1, max_length=200)] = Field(
        description="custom_node_id, or node_id, from the template"
    )
    open_ids: OpenIds


class Submit(Template):
    form: list[FormValue] = Field(
        min_length=1, max_length=100, description="One entry per form widget: id, type, value"
    )
    approvers: list[NodeUsers] = Field(
        default_factory=list, max_length=20, description="Only for nodes the submitter chooses"
    )
    cc: list[NodeUsers] = Field(default_factory=list, max_length=20)
    uuid: Uuid


class Remind(Instance):
    task_ids: Annotated[list[Identifier], Field(min_length=1, max_length=20)]
    comment: Comment | None = None


class Transfer(Decision):
    transfer_open_id: Identifier


def _nodes(items: list[NodeUsers]) -> list[dict[str, Any]] | None:
    return [{"key": item.key, "value": item.open_ids} for item in items] or None


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


@tool(
    "feishu_get_user",
    UserRef,
    "contact.user",
    "Read a colleague's profile by open_id: email, departments, manager and title.",
)
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


@tool(
    "feishu_approval_transfer",
    Transfer,
    "approval.transfer",
    "Hand the user's pending approval task to a colleague (open_id).",
)
async def transfer(args: Transfer, call: Call) -> dict[str, Any]:
    body = compact(
        {
            "instance_code": args.instance_code,
            "task_id": args.task_id,
            "transfer_user_id": args.transfer_open_id,
            "comment": args.comment,
        }
    )
    return await call("approval.transfer", params={"user_id_type": "open_id"}, json=body)


@tool(
    "feishu_approval_templates",
    Templates,
    "approval.templates",
    "Find approval forms the user can submit, such as leave, expense or purchase.",
)
async def templates(args: Templates, call: Call) -> dict[str, Any]:
    body = compact(
        {
            "keyword": args.keyword or None,
            "locale": "zh-CN",
            "page_size": args.limit,
            "page_token": args.page_token,
        }
    )
    return await call("approval.templates", json=body)


@tool(
    "feishu_approval_template",
    Template,
    "approval.template",
    "Read an approval form's widgets (id, type, options, required) and approval nodes.",
)
async def template(args: Template, call: Call) -> dict[str, Any]:
    data = await call(
        "approval.template", path={"approval_code": args.approval_code}, params={"locale": "zh-CN"}
    )
    if isinstance(data.get("form"), str):
        try:
            data = {**data, "form": json.loads(data["form"])}
        except ValueError:
            pass
    return data


@tool(
    "feishu_approval_submit",
    Submit,
    "approval.submit",
    "Submit an approval as the user with the form values read from its template."
    " Reuse uuid for retries.",
)
async def submit(args: Submit, call: Call) -> dict[str, Any]:
    form = [item.model_dump() for item in args.form]
    body = compact(
        {
            "approval_code": args.approval_code,
            "form": json.dumps(form, ensure_ascii=False),
            "node_approver_list": _nodes(args.approvers),
            "node_cc_list": _nodes(args.cc),
            "uuid": args.uuid,
        }
    )
    return await call("approval.submit", json=body)


@tool(
    "feishu_approval_recall",
    Instance,
    "approval.recall",
    "Withdraw an approval the user submitted.",
)
async def recall(args: Instance, call: Call) -> dict[str, Any]:
    return await call("approval.recall", json={"instance_code": args.instance_code})


@tool(
    "feishu_approval_remind",
    Remind,
    "approval.remind",
    "Remind the approvers of the user's submitted approval (task IDs from its details).",
)
async def remind(args: Remind, call: Call) -> dict[str, Any]:
    return await call("approval.remind", json=args.model_dump(exclude_none=True))


@tool(
    "feishu_search_departments",
    SearchDepartments,
    "contact.departments",
    "Find departments by name to get their IDs.",
)
async def search_departments(args: SearchDepartments, call: Call) -> dict[str, Any]:
    params = {**page(args), "department_id_type": "open_department_id", "user_id_type": "open_id"}
    return await call("contact.departments", params=params, json={"query": args.query})


@tool(
    "feishu_get_department",
    DepartmentRef,
    "contact.department",
    "Read a department: name, leader and parent.",
)
async def get_department(args: DepartmentRef, call: Call) -> dict[str, Any]:
    return await call(
        "contact.department",
        path={"department_id": args.department_id},
        params={"department_id_type": "open_department_id", "user_id_type": "open_id"},
    )


@tool(
    "feishu_department_members",
    DepartmentMembers,
    "contact.department_users",
    "List the people directly in a department.",
)
async def department_members(args: DepartmentMembers, call: Call) -> dict[str, Any]:
    params = {
        **page(args),
        "department_id": args.department_id,
        "department_id_type": "open_department_id",
        "user_id_type": "open_id",
    }
    return await call("contact.department_users", params=params)


@tool(
    "feishu_okr_add_progress",
    AddProgress,
    "okr.progress_create",
    "Record progress on one of the user's objectives or key results.",
)
async def okr_add_progress(args: AddProgress, call: Call) -> dict[str, Any]:
    body = {
        **args.body(),
        "target_id": args.target_id,
        "target_type": 2 if args.target_type == "objective" else 3,
        "source_title": "CoreMan",
        "source_url": "https://open.feishu.cn/app",
    }
    return await call("okr.progress_create", params={"user_id_type": "open_id"}, json=body)


@tool(
    "feishu_okr_update_progress",
    UpdateProgress,
    "okr.progress_update",
    "Change a progress record the user wrote.",
)
async def okr_update_progress(args: UpdateProgress, call: Call) -> dict[str, Any]:
    return await call(
        "okr.progress_update",
        path={"progress_id": args.progress_id},
        params={"user_id_type": "open_id"},
        json=args.body(),
    )


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
    search_departments,
    get_department,
    department_members,
    approval_tasks,
    approval_initiated,
    approval_instance,
    approve,
    reject,
    transfer,
    templates,
    template,
    submit,
    recall,
    remind,
    okr_cycles,
    okr_objectives,
    okr_key_results,
    okr_add_progress,
    okr_update_progress,
    attendance,
]
