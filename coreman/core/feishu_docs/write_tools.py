"""群里以提问人身份调用个人工具：判权、判风险、审计、去重。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import FeishuGroupAction
from coreman.core.feishu_docs import access, confirm
from coreman.core.feishu_docs import write_policy as wp
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.feishu_personal import endpoints, inflow
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call, bounded
from coreman.core.feishu_personal.tools import TOOLS


def record(
    session: AsyncSession,
    scope: DocsScope,
    name: str,
    category: str,
    target: str | None,
    status: str,
    digest: str,
    upstream: int | None = None,
) -> None:
    assert scope.speaker_user_id is not None
    session.add(
        FeishuGroupAction(
            bot_id=scope.bot.id,
            user_id=scope.speaker_user_id,
            chat_id=scope.chat_id,
            task_id=scope.task.id,
            tool=name,
            target=target,
            category=category,
            status=status,
            upstream_code=upstream,
            args_hash=digest,
        )
    )


async def _trusted(call: Call, scope: DocsScope, target: str) -> bool:
    """目标来自提问人本条消息的链接，或本轮刚新建。知识库节点要解析成实际文档。"""
    links = [str(t) for t in (scope.task.payload.get("doc_links") or [])]
    if target in links:
        return True
    created = await call.session.scalar(
        select(FeishuGroupAction.id)
        .where(
            FeishuGroupAction.task_id == scope.task.id,
            FeishuGroupAction.category == "create",
            FeishuGroupAction.status == "succeeded",
            FeishuGroupAction.target == target,
        )
        .limit(1)
    )
    if created is not None:
        return True
    for token in links[:10]:
        try:
            node = (await call("wiki.node", params={"token": token})).get("node") or {}
        except PersonalError:
            continue
        if node.get("obj_token") == target:
            return True
    return False


def _created_token(data: dict[str, Any]) -> str | None:
    for holder in (
        data,
        data.get("document") or {},
        data.get("node") or {},
        data.get("spreadsheet") or {},
        data.get("task") or {},
        data.get("event") or {},
    ):
        for key in ("document_id", "obj_token", "spreadsheet_token", "guid", "event_id", "token"):
            value = holder.get(key) if isinstance(holder, dict) else None
            if isinstance(value, str) and value:
                return value
    return None


async def dispatch(
    session: AsyncSession,
    cipher: Cipher,
    scope: DocsScope,
    name: str,
    arguments: dict[str, Any],
    files: FileRelay | None,
) -> dict[str, Any]:
    if scope.speaker_user_id is None:
        return {"error": "no_access", "reason": "unknown_speaker"}
    item = TOOLS[name]
    try:
        args: Any = item.args.model_validate(arguments)
    except ValidationError as exc:
        problems = [
            ".".join(str(p) for p in e["loc"]) + ": " + e["msg"]
            for e in exc.errors(include_url=False, include_input=False)[:5]
        ]
        return {"error": "invalid_tool_or_arguments", "invalid": problems}
    if name == "feishu_edit_document" and args.command == "overwrite":
        return {"error": "overwrite_not_allowed_in_group"}
    who = await access.requester(session, scope)
    if who is None or not who.reaches(item.endpoint):
        # 只代发类接口要「全部」档；用 general 去请求，只读不发档的人永远满足不了。
        send = endpoints.ENDPOINTS[item.endpoint].kind == "send"
        result = await inflow.request(
            session,
            cipher,
            access.group_scope(cipher, scope),
            need="send_messages" if send else "general",
            purpose="操作飞书",
            endpoints_needed=(item.endpoint,),
        )
        if result.get("status") == "already_connected":
            # 上面刚判过够不着（如已不在机器人使用者名单里），不能当作授权成功。
            return {"error": "no_access", "reason": "requester_unavailable"}
        return result
    call = Call(session, cipher, who.scope, files)
    category = wp.effective_category(name, args)
    if category == "locate":
        return bounded(
            {
                **wp.trim_locate(await item.run(args, call)),
                "content_trust": "external_untrusted_data",
            }
        )
    digest = wp.args_hash(arguments)
    field = wp.TARGET_FIELD.get(name)
    target = str(getattr(args, field)) if field else None
    done = (
        await session.execute(
            select(FeishuGroupAction.id, FeishuGroupAction.target)
            .where(
                FeishuGroupAction.task_id == scope.task.id,
                FeishuGroupAction.tool == name,
                FeishuGroupAction.args_hash == digest,
                FeishuGroupAction.status == "succeeded",
            )
            .limit(1)
        )
    ).first()
    if done is not None:
        return {"status": "already_done", "target": done.target}
    count = await session.scalar(
        select(func.count())
        .select_from(FeishuGroupAction)
        .where(
            FeishuGroupAction.task_id == scope.task.id,
            FeishuGroupAction.status == "succeeded",
        )
    )
    if (count or 0) >= wp.MAX_WRITES_PER_TURN:
        return {"error": "write_limit_reached"}
    if category == "target" and not (target and await _trusted(call, scope, target)):
        category = "confirm"
    if category == "confirm":
        return await confirm.propose(session, scope, name, arguments, target, digest)
    try:
        data = await item.run(args, call)
    except PersonalError as exc:
        record(session, scope, name, category, target, "failed", digest, exc.upstream_code)
        return exc.payload()
    record(session, scope, name, category, target or _created_token(data), "succeeded", digest)
    return bounded({**data, "content_trust": "external_untrusted_data"})
