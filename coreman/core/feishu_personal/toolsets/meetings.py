"""会议、妙记、会议纪要：搜索只限本人参加或拥有的记录。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import StringConstraints

from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Identifier,
    Search,
    TextPage,
    page,
    text_page,
    tool,
)


class SearchMeetings(Search):
    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] = ""


class SearchMinutes(Search):
    relationship: Literal["participant", "owner"] = "participant"


class ReadMeeting(Arguments):
    meeting_id: Identifier


class ReadMinutes(TextPage):
    minute_token: Identifier


class ReadNote(Arguments):
    note_id: Identifier


@tool("feishu_search_meetings", SearchMeetings, "vc.search", "Search meetings the user attended.")
async def search_meetings(args: SearchMeetings, call: Call) -> dict[str, Any]:
    own: dict[str, Any] = {"participant_ids": [call.scope.open_id]}
    if args.time_range():
        own["start_time"] = args.time_range()
    body: dict[str, Any] = {"query": args.query} if args.query else {}
    body["meeting_filter"] = own
    return await call("vc.search", params=page(args), json=body)


@tool("feishu_read_meeting", ReadMeeting, "vc.meeting", "Read an accessible meeting by ID.")
async def read_meeting(args: ReadMeeting, call: Call) -> dict[str, Any]:
    return await call("vc.meeting", path={"meeting_id": args.meeting_id})


@tool(
    "feishu_search_minutes",
    SearchMinutes,
    "minutes.search",
    "Search minutes the user took part in or owns.",
)
async def search_minutes(args: SearchMinutes, call: Call) -> dict[str, Any]:
    key = "owner_ids" if args.relationship == "owner" else "participant_ids"
    own: dict[str, Any] = {key: [call.scope.open_id]}
    if args.time_range():
        own["create_time"] = args.time_range()
    body: dict[str, Any] = {"query": args.query} if args.query else {}
    body.update({"filter": own, "sorter": "create_time_desc"})
    return await call("minutes.search", params=page(args), json=body)


@tool(
    "feishu_read_minutes",
    ReadMinutes,
    "minutes.get",
    "Read accessible minutes and transcript artifacts.",
)
async def read_minutes(args: ReadMinutes, call: Call) -> dict[str, Any]:
    path = {"minute_token": args.minute_token}
    data = await call("minutes.get", path=path)
    artifacts = await call("minutes.artifacts", path=path)
    transcript = artifacts.get("transcript")
    paging: dict[str, Any] = {}
    if isinstance(transcript, str):
        text, paging = text_page(transcript, args)
        artifacts = {**artifacts, "transcript": text}
    return {**data, "artifacts": artifacts, **paging}


@tool("feishu_read_note", ReadNote, "vc.note", "Read an accessible meeting note by ID.")
async def read_note(args: ReadNote, call: Call) -> dict[str, Any]:
    return await call("vc.note", path={"note_id": args.note_id})


TOOLS = [search_meetings, read_meeting, search_minutes, read_minutes, read_note]
