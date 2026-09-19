"""日程：默认操作本人主日历；查询、忙闲、创建、修改、删除、邀请与回复。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from coreman.core.feishu_personal import service
from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Identifier,
    ISOTime,
    OpenIds,
    Page,
    PathId,
    Short,
    Uuid,
    compact,
    ordered,
    page,
    seconds,
    tool,
    valid_times,
)

Description = Annotated[str, StringConstraints(max_length=5000)]
Location = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
_CALENDAR = Field(default=None, description="Omit for the user's primary calendar")


class OnCalendar(Arguments):
    calendar_id: PathId | None = _CALENDAR


class TimeRange(OnCalendar):
    start_time: ISOTime
    end_time: ISOTime

    _times = valid_times("start_time", "end_time")

    @model_validator(mode="after")
    def ordered_times(self) -> TimeRange:
        ordered(self.start_time, self.end_time)
        start, end = datetime.fromisoformat(self.start_time), datetime.fromisoformat(self.end_time)
        if end - start > timedelta(days=40):
            raise ValueError("at most 40 days")
        return self


class EventRef(OnCalendar):
    event_id: PathId


class SearchEvents(Page, OnCalendar):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    start_time: ISOTime | None = None
    end_time: ISOTime | None = None

    _times = valid_times("start_time", "end_time")

    @model_validator(mode="after")
    def ordered_times(self) -> SearchEvents:
        ordered(self.start_time, self.end_time)
        return self


class FreeBusy(Arguments):
    start_time: ISOTime
    end_time: ISOTime
    user_open_id: Identifier | None = Field(default=None, description="Omit for the user")

    _times = valid_times("start_time", "end_time")

    @model_validator(mode="after")
    def ordered_times(self) -> FreeBusy:
        ordered(self.start_time, self.end_time)
        return self


class EventFields(OnCalendar):
    summary: Short | None = None
    description: Description | None = None
    start_time: ISOTime | None = None
    end_time: ISOTime | None = None
    location: Location | None = None
    video_meeting: bool | None = Field(default=None, description="Attach a Feishu meeting link")
    need_notification: bool = True

    _times = valid_times("start_time", "end_time")

    @model_validator(mode="after")
    def ordered_times(self) -> EventFields:
        ordered(self.start_time, self.end_time)
        return self

    def body(self) -> dict[str, Any]:
        return compact(
            {
                "summary": self.summary,
                "description": self.description,
                "start_time": {"timestamp": str(seconds(self.start_time))}
                if self.start_time
                else None,
                "end_time": {"timestamp": str(seconds(self.end_time))} if self.end_time else None,
                "location": {"name": self.location} if self.location else None,
                "vchat": None
                if self.video_meeting is None
                else {"vc_type": "vc" if self.video_meeting else "no_meeting"},
                "need_notification": self.need_notification,
            }
        )


class CreateEvent(EventFields):
    summary: Short
    start_time: ISOTime
    end_time: ISOTime
    attendee_open_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    idempotency_key: Uuid


class UpdateEvent(EventFields):
    event_id: PathId

    @model_validator(mode="after")
    def has_change(self) -> UpdateEvent:
        if not {"summary", "description", "start_time", "end_time", "location", "video_meeting"} & (
            self.model_fields_set
        ):
            raise ValueError("nothing to update")
        return self


class DeleteEvent(EventRef):
    need_notification: bool = True


class AddAttendees(EventRef):
    attendee_open_ids: OpenIds
    need_notification: bool = True


class ReplyEvent(EventRef):
    rsvp_status: Literal["accept", "decline", "tentative"]


async def _calendar(call: Call, calendar_id: str | None) -> str:
    if calendar_id is not None:
        return calendar_id
    data = await call("calendar.primary", params={"user_id_type": "open_id"})
    for item in data.get("calendars") or []:
        found = (item.get("calendar") or {}).get("calendar_id") if isinstance(item, dict) else None
        if isinstance(found, str) and found:
            return found
    raise service.PersonalError("primary_calendar_unavailable")


def _attendees(open_ids: list[str], notify: bool) -> dict[str, Any]:
    return {
        "attendees": [{"type": "user", "user_id": open_id} for open_id in open_ids],
        "need_notification": notify,
    }


@tool("feishu_calendar_list", Page, "calendar.list", "List calendars the user can access.")
async def list_calendars(args: Page, call: Call) -> dict[str, Any]:
    # This API's page size starts at 50; the page is trimmed to `limit` afterwards.
    return await call("calendar.list", params=compact({"page_token": args.page_token}))


@tool(
    "feishu_calendar_events",
    TimeRange,
    "calendar.instances",
    "List events, with recurring ones expanded, in a time range of up to 40 days.",
)
async def list_events(args: TimeRange, call: Call) -> dict[str, Any]:
    calendar = await _calendar(call, args.calendar_id)
    return await call(
        "calendar.instances",
        path={"calendar_id": calendar},
        params={
            "start_time": str(seconds(args.start_time)),
            "end_time": str(seconds(args.end_time)),
            "user_id_type": "open_id",
        },
    )


@tool("feishu_calendar_event", EventRef, "calendar.event", "Read one event with its attendees.")
async def read_event(args: EventRef, call: Call) -> dict[str, Any]:
    calendar = await _calendar(call, args.calendar_id)
    return await call(
        "calendar.event",
        path={"calendar_id": calendar, "event_id": args.event_id},
        params={"need_attendee": "true", "max_attendee_num": 100, "user_id_type": "open_id"},
    )


@tool(
    "feishu_calendar_search",
    SearchEvents,
    "calendar.search",
    "Search events by title keyword, optionally within a time range.",
)
async def search_events(args: SearchEvents, call: Call) -> dict[str, Any]:
    calendar = await _calendar(call, args.calendar_id)
    body: dict[str, Any] = {"query": args.query}
    times = compact({"start_time": args.start_time, "end_time": args.end_time})
    if times:
        body["filter"] = {"time_range": times}
    return await call(
        "calendar.search",
        path={"calendar_id": calendar},
        params={**page(args), "user_id_type": "open_id"},
        json=body,
    )


@tool(
    "feishu_calendar_freebusy",
    FreeBusy,
    "calendar.freebusy",
    "Busy periods of the user or a colleague (open_id) in a time range.",
)
async def freebusy(args: FreeBusy, call: Call) -> dict[str, Any]:
    return await call(
        "calendar.freebusy",
        params={"user_id_type": "open_id"},
        json={
            "time_min": args.start_time,
            "time_max": args.end_time,
            "user_id": args.user_open_id or call.scope.open_id,
        },
    )


@tool(
    "feishu_calendar_create_event",
    CreateEvent,
    "calendar.create",
    "Create an event and invite attendees (open_id). Reuse idempotency_key for retries.",
)
async def create_event(args: CreateEvent, call: Call) -> dict[str, Any]:
    calendar = await _calendar(call, args.calendar_id)
    created = await call(
        "calendar.create",
        path={"calendar_id": calendar},
        params={"user_id_type": "open_id", "idempotency_key": args.idempotency_key},
        json=args.body(),
    )
    event_id = (created.get("event") or {}).get("event_id")
    if not args.attendee_open_ids or not isinstance(event_id, str):
        return created
    try:
        invited = await call(
            "calendar.attendees",
            path={"calendar_id": calendar, "event_id": event_id},
            params={"user_id_type": "open_id"},
            json=_attendees(args.attendee_open_ids, args.need_notification),
        )
    except service.PersonalError as exc:
        # The event exists; say so instead of inviting the model to create it again.
        return {**created, "attendees_error": exc.payload()}
    return {**created, "attendees": invited.get("attendees")}


@tool(
    "feishu_calendar_update_event",
    UpdateEvent,
    "calendar.update",
    "Change an event's title, description, time, place or meeting link.",
)
async def update_event(args: UpdateEvent, call: Call) -> dict[str, Any]:
    calendar = await _calendar(call, args.calendar_id)
    return await call(
        "calendar.update",
        path={"calendar_id": calendar, "event_id": args.event_id},
        params={"user_id_type": "open_id"},
        json=args.body(),
    )


@tool("feishu_calendar_delete_event", DeleteEvent, "calendar.delete", "Delete an event.")
async def delete_event(args: DeleteEvent, call: Call) -> dict[str, Any]:
    calendar = await _calendar(call, args.calendar_id)
    return await call(
        "calendar.delete",
        path={"calendar_id": calendar, "event_id": args.event_id},
        params={"need_notification": "true" if args.need_notification else "false"},
    )


@tool(
    "feishu_calendar_add_attendees",
    AddAttendees,
    "calendar.attendees",
    "Invite people (open_id) to an existing event.",
)
async def add_attendees(args: AddAttendees, call: Call) -> dict[str, Any]:
    calendar = await _calendar(call, args.calendar_id)
    return await call(
        "calendar.attendees",
        path={"calendar_id": calendar, "event_id": args.event_id},
        params={"user_id_type": "open_id"},
        json=_attendees(args.attendee_open_ids, args.need_notification),
    )


@tool(
    "feishu_calendar_reply",
    ReplyEvent,
    "calendar.reply",
    "Accept, decline or tentatively accept an invitation.",
)
async def reply_event(args: ReplyEvent, call: Call) -> dict[str, Any]:
    calendar = await _calendar(call, args.calendar_id)
    return await call(
        "calendar.reply",
        path={"calendar_id": calendar, "event_id": args.event_id},
        json={"rsvp_status": args.rsvp_status},
    )


TOOLS = [
    list_calendars,
    list_events,
    read_event,
    search_events,
    freebusy,
    create_event,
    update_event,
    delete_event,
    add_attendees,
    reply_event,
]
