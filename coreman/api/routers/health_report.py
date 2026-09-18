"""机器人管理员的独立流式体检；不复用正常会话，不接受伪造调用人。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.auth.system_access import build_system_access
from coreman.core.cron.access import require_operator
from coreman.core.db.models import Bot, RelayServer, User
from coreman.core.knowledge.installation import effective_env
from coreman.core.prompting import build_env
from coreman.core.prompting.system_prompt import build_system_prompt, load_segments
from coreman.core.relay.client import ChatRequest, RelayClient
from coreman.core.relay.models import backend_of
from coreman.core.relay.sse import FinishEvent, RelayErrorEvent, TextDelta, ToolUseStart, UsageEvent

router = APIRouter(
    prefix="/api/admin/bots", tags=["health-report"], dependencies=[Depends(verify_csrf)]
)
PROMPT = (
    "请只读检查当前运行环境中的机器人工作目录、已安装技能和可用工具，给出简洁的环境检查报告："
    "正常项、失败项、未能验证的项。禁止修改文件、安装依赖、发送消息或执行写入操作；"
    "不要显示任何密码、令牌、密钥或环境变量值。不要要求用户回答交互问题。"
    "仅报告此运行环境中实际检查到的结果，不要声称已验证外部平台或端到端链路。"
)
LIMIT = 128 * 1024


def event(kind: str, data: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class OutputFilter:
    """保留足够尾部，避免已知凭据跨 chunk 被分段输出。"""

    def __init__(self, values: dict[str, str]):
        self.secrets = sorted(
            {
                value
                for key, value in values.items()
                if len(value) >= 8
                and any(
                    word in key.upper()
                    for word in ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY")
                )
            },
            key=len,
            reverse=True,
        )
        self.keep = max((len(value) for value in self.secrets), default=1) - 1
        self.buffer = ""

    def feed(self, content: str, *, final: bool = False) -> str:
        self.buffer += content
        boundary = len(self.buffer) if final else max(0, len(self.buffer) - self.keep)
        output = []
        index = 0
        while index < boundary:
            match = next(
                (value for value in self.secrets if self.buffer.startswith(value, index)), None
            )
            if match:
                output.append("[REDACTED]")
                index += len(match)
            else:
                output.append(self.buffer[index])
                index += 1
        self.buffer = self.buffer[index:]
        return "".join(output)


def make_client(relay: RelayServer) -> RelayClient:
    return RelayClient.for_relay(relay)


@router.post("/{bot_id}/health-report")
async def health_report(
    bot_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    bot = await session.get(Bot, bot_id)
    if bot is None:
        raise not_found("机器人不存在")
    await require_operator(session, bot, actor)
    if not bot.relay_server_id:
        raise ApiError(409, 409, "机器人尚未分配实例")
    report_id = uuid.uuid4()
    await record_audit(
        session,
        action="bot.health_report_requested",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot",
        target_id=str(bot_id),
        diff={"report_id": [None, str(report_id)]},
        ip=client_ip(request),
    )
    await session.commit()
    actor_id = actor.id
    # 流结束时请求对象仍可用，但先取出 IP，完成审计与发起审计保持同一来源。
    ip = client_ip(request)

    async def stream() -> AsyncIterator[str]:
        status = "failed"
        client = None
        # 跨 API 实例最多两个体检，每个机器人最多一个；事务结束自动释放锁。
        async with request.app.state.engine.connect() as connection:
            async with connection.begin():
                locked = await connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"health-report:{bot_id}"},
                )
                slot = False
                if locked:
                    for index in range(2):
                        slot = bool(
                            await connection.scalar(
                                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                                {"key": f"health-report-global:{index}"},
                            )
                        )
                        if slot:
                            break
                if not locked or not slot:
                    yield event("error", {"message": "已有运行环境检查正在进行，请稍后重试。"})
                    return
                try:
                    async with request.app.state.session_factory() as fresh:
                        current_bot = await fresh.get(Bot, bot_id)
                        current_actor = await fresh.get(User, actor_id)
                        if current_bot is None or current_actor is None:
                            raise ValueError("owner_missing")
                        speaker = await require_operator(fresh, current_bot, current_actor)
                        relay = (
                            await fresh.get(RelayServer, current_bot.relay_server_id)
                            if current_bot.relay_server_id
                            else None
                        )
                        if relay is None or not relay.is_active:
                            raise ValueError("relay_unavailable")
                        store = request.app.state.settings_store
                        access = await build_system_access(
                            fresh,
                            request.app.state.cipher,
                            bot=current_bot,
                            speaker=speaker,
                            issuer=str(await store.get("jwt_issuer", default="coreman")),
                            external_key=request.app.state.settings.external_jwt_key,
                        )
                        env = build_env(
                            bot_key=current_bot.bot_key,
                            platform=current_bot.platform,
                            chat_id=f"health:{report_id}",
                            chat_type="health",
                            platform_user_id=speaker.platform_user_id,
                            session_id="",
                            speaker=speaker,
                            bot_env=await effective_env(
                                fresh, request.app.state.cipher, current_bot
                            ),
                        )
                        env.update(access.env)
                        backend = backend_of(current_bot.model, relay.model_provider)
                        chat = ChatRequest(
                            model=current_bot.model,
                            system_prompt=build_system_prompt(
                                segments=await load_segments(store),
                                backend=backend,
                                verbosity_level=1,
                                bot_prompt=current_bot.merged_system_prompt,
                                speaker=speaker,
                                speaker_changed=False,
                                systems_prompt=access.prompt,
                            ),
                            user_content=PROMPT
                            + (
                                " 日本語で回答してください。"
                                if current_actor.locale == "ja"
                                else ""
                            ),
                            working_dir=current_bot.working_dir,
                            session_id="",
                            backend=backend,
                            env_vars=env,
                            max_turns=8,
                        )
                        bot_version = current_bot.version
                        await fresh.commit()
                    yield event("start", {"report_id": str(report_id)})
                    client = make_client(relay)
                    output = OutputFilter(env)
                    size, finished, text_seen = 0, False, False
                    async with (
                        asyncio.timeout(300),
                        aclosing(
                            client.chat_stream(chat, total_timeout=300, read_timeout=60)
                        ) as events,
                    ):
                        async for item in events:
                            if await request.is_disconnected():
                                status = "cancelled"
                                return
                            if isinstance(item, TextDelta):
                                size += len(item.text.encode())
                                if size > LIMIT:
                                    raise ValueError("report_too_large")
                                text_seen = text_seen or bool(item.text.strip())
                                chunk = output.feed(item.text)
                                if chunk:
                                    yield event("text", {"text": chunk})
                            elif isinstance(item, ToolUseStart):
                                # 工具名只是状态，不输出工具参数或返回值。
                                yield event("tool", {"name": item.name[:100]})
                            elif isinstance(item, UsageEvent):
                                yield event(
                                    "usage",
                                    {
                                        "input_tokens": item.input_tokens,
                                        "output_tokens": item.output_tokens,
                                    },
                                )
                            elif isinstance(item, RelayErrorEvent):
                                raise ValueError("relay_failed")
                            elif isinstance(item, FinishEvent):
                                finished = item.reason in {"stop", "end_turn", "completed"}
                    if not finished or not text_seen:
                        raise ValueError("incomplete_report")
                    tail = output.feed("", final=True)
                    if tail:
                        yield event("text", {"text": tail})
                    async with request.app.state.session_factory() as fresh:
                        latest = await fresh.get(Bot, bot_id)
                        if latest is None or latest.version != bot_version:
                            raise ValueError("configuration_changed_during_report")
                    status = "succeeded"
                    yield event("done", {"report_id": str(report_id)})
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                except Exception:
                    yield event(
                        "error",
                        {
                            "message": (
                                "运行环境检查未完整完成或配置已变更，请稍后重试；"
                                "上方内容仅为部分结果。"
                            )
                        },
                    )
                finally:
                    if client is not None:
                        await client.aclose()
                    async with request.app.state.session_factory() as audit_session:
                        await record_audit(
                            audit_session,
                            action="bot.health_report_finished",
                            actor_id=actor_id,
                            target_type="bot",
                            target_id=str(bot_id),
                            diff={"report_id": [None, str(report_id)], "status": [None, status]},
                            ip=ip,
                        )
                        await audit_session.commit()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
