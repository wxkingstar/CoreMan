"""任务：本人负责的任务与清单；创建、修改、完成、删除、评论。"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Day,
    Identifier,
    ISOTime,
    Page,
    Uuid,
    compact,
    millis,
    page,
    tool,
    valid_times,
)

Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]
Detail = Annotated[str, StringConstraints(max_length=30000)]
_DUE = Field(default=None, description="ISO time, or YYYY-MM-DD for an all-day due date")


class ListTasks(Page):
    completed: bool | None = Field(default=None, description="Omit for both")
    relation: Literal["responsible", "related"] = Field(
        default="responsible",
        description="related also includes tasks the user created or follows",
    )


class SearchTasks(Page):
    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    creator_open_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    assignee_open_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    follower_open_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    completed: bool | None = None
    due_after: ISOTime | None = None
    due_before: ISOTime | None = None

    _due = valid_times("due_after", "due_before")

    @model_validator(mode="after")
    def something(self) -> SearchTasks:
        if not (
            self.query
            or self.creator_open_ids
            or self.assignee_open_ids
            or self.follower_open_ids
            or self.completed is not None
            or self.due_after
            or self.due_before
        ):
            raise ValueError("give a query or at least one filter")
        return self


class TaskRef(Arguments):
    task_guid: Identifier


class TasklistTasks(ListTasks):
    tasklist_guid: Identifier


class TaskFields(Arguments):
    summary: Summary
    description: Detail | None = None
    due: ISOTime | Day | None = _DUE
    remind_minutes_before: int | None = Field(
        default=None, ge=0, le=20160, description="Remind before the due time; needs due"
    )
    assignee_open_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    follower_open_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    client_token: Uuid

    _due = valid_times("due")

    @model_validator(mode="after")
    def reminder_needs_due(self) -> TaskFields:
        if self.remind_minutes_before is not None and not self.due:
            raise ValueError("remind_minutes_before needs due")
        return self

    def body(self) -> dict[str, Any]:
        return compact(
            {
                "summary": self.summary,
                "description": self.description,
                "due": _due(self.due) if self.due else None,
                "reminders": None
                if self.remind_minutes_before is None
                else [{"relative_fire_minute": self.remind_minutes_before}],
                "members": _members(self) or None,
                "client_token": self.client_token,
            }
        )


class CreateTask(TaskFields):
    tasklist_guid: Identifier | None = None


class Subtask(TaskFields):
    parent_task_guid: Identifier


class UpdateTask(TaskRef):
    summary: Summary | None = None
    description: Detail | None = None
    due: ISOTime | Day | None = _DUE
    completed: bool | None = None

    _due = valid_times("due")

    @model_validator(mode="after")
    def has_change(self) -> UpdateTask:
        if not {"summary", "description", "due", "completed"} & self.model_fields_set:
            raise ValueError("nothing to update")
        return self


class Comment(TaskRef):
    content: Annotated[str, StringConstraints(min_length=1, max_length=3000)]


def _due(value: str) -> dict[str, Any]:
    return {"timestamp": str(millis(value)), "is_all_day": len(value) == 10}


def _members(args: TaskFields) -> list[dict[str, str]]:
    return [
        {"id": open_id, "type": "user", "role": role}
        for role, ids in (
            ("assignee", args.assignee_open_ids),
            ("follower", args.follower_open_ids),
        )
        for open_id in ids
    ]


@tool(
    "feishu_task_list",
    ListTasks,
    "task.list",
    "List tasks the user is responsible for, or every task they are related to.",
)
async def list_tasks(args: ListTasks, call: Call) -> dict[str, Any]:
    params = {**page(args), "user_id_type": "open_id"}
    if args.completed is not None:
        params["completed"] = "true" if args.completed else "false"
    if args.relation == "related":
        return await call("task.related", params=params)
    return await call("task.list", params={**params, "type": "my_tasks"})


@tool(
    "feishu_task_search",
    SearchTasks,
    "task.search",
    "Search tasks by keyword, creator, assignee, follower, completion or due time.",
)
async def search_tasks(args: SearchTasks, call: Call) -> dict[str, Any]:
    due = compact({"start_time": args.due_after, "end_time": args.due_before})
    filters = compact(
        {
            "creator_ids": args.creator_open_ids or None,
            "assignee_ids": args.assignee_open_ids or None,
            "follower_ids": args.follower_open_ids or None,
            "is_completed": args.completed,
            "due_time": due or None,
        }
    )
    params = compact({"page_token": args.page_token, "user_id_type": "open_id"})
    return await call(
        "task.search", params=params, json=compact({"query": args.query, "filter": filters or None})
    )


@tool("feishu_task_get", TaskRef, "task.get", "Read one task with members and due date.")
async def get_task(args: TaskRef, call: Call) -> dict[str, Any]:
    return await call(
        "task.get", path={"task_guid": args.task_guid}, params={"user_id_type": "open_id"}
    )


@tool(
    "feishu_task_create",
    CreateTask,
    "task.create",
    "Create a task with a due date, reminder, assignees and followers (open_id)."
    " Reuse client_token for retries.",
)
async def create_task(args: CreateTask, call: Call) -> dict[str, Any]:
    body = args.body()
    if args.tasklist_guid:
        body["tasklists"] = [{"tasklist_guid": args.tasklist_guid}]
    return await call("task.create", params={"user_id_type": "open_id"}, json=body)


@tool(
    "feishu_task_add_subtask",
    Subtask,
    "task.subtask",
    "Add a subtask under a task. Reuse client_token for retries.",
)
async def add_subtask(args: Subtask, call: Call) -> dict[str, Any]:
    return await call(
        "task.subtask",
        path={"task_guid": args.parent_task_guid},
        params={"user_id_type": "open_id"},
        json=args.body(),
    )


@tool(
    "feishu_task_update",
    UpdateTask,
    "task.update",
    "Change a task's title, description or due date, or mark it done or not done.",
)
async def update_task(args: UpdateTask, call: Call) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "summary" in args.model_fields_set and args.summary is not None:
        fields["summary"] = args.summary
    if "description" in args.model_fields_set:
        fields["description"] = args.description or ""
    if "due" in args.model_fields_set:
        fields["due"] = _due(args.due) if args.due else None
    if args.completed is not None:
        fields["completed_at"] = str(int(time.time() * 1000)) if args.completed else "0"
    return await call(
        "task.update",
        path={"task_guid": args.task_guid},
        params={"user_id_type": "open_id"},
        json={"task": compact(fields), "update_fields": sorted(fields)},
    )


@tool("feishu_task_delete", TaskRef, "task.delete", "Delete a task.")
async def delete_task(args: TaskRef, call: Call) -> dict[str, Any]:
    return await call("task.delete", path={"task_guid": args.task_guid})


@tool("feishu_tasklists", Page, "task.tasklists", "List the user's task lists.")
async def tasklists(args: Page, call: Call) -> dict[str, Any]:
    return await call("task.tasklists", params={**page(args), "user_id_type": "open_id"})


@tool("feishu_tasklist_tasks", TasklistTasks, "task.tasklist_tasks", "List a task list's tasks.")
async def tasklist_tasks(args: TasklistTasks, call: Call) -> dict[str, Any]:
    params = {**page(args), "user_id_type": "open_id"}
    if args.completed is not None:
        params["completed"] = "true" if args.completed else "false"
    return await call(
        "task.tasklist_tasks", path={"tasklist_guid": args.tasklist_guid}, params=params
    )


@tool("feishu_task_comment", Comment, "task.comment", "Comment on a task as the user.")
async def comment(args: Comment, call: Call) -> dict[str, Any]:
    return await call(
        "task.comment",
        params={"user_id_type": "open_id"},
        json={"content": args.content, "resource_type": "task", "resource_id": args.task_guid},
    )


TOOLS = [
    list_tasks,
    search_tasks,
    get_task,
    create_task,
    add_subtask,
    update_task,
    delete_task,
    tasklists,
    tasklist_tasks,
    comment,
]
