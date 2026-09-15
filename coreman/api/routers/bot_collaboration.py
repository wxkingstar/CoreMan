"""Task-scoped help endpoint; no user-supplied actor or arbitrary destination."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.errors import ApiError
from coreman.core.chat import bot_collaboration as service

router = APIRouter(tags=["bot-collaboration"])


class HelpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_bot_key: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=4000)


@router.post("/api/runtime/bot-help")
async def request_help(
    body: HelpIn, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 4096:
        raise ApiError(401, 401, "求助凭证无效")
    try:
        task_id, actor = service.read_capability(request.app.state.cipher, auth[7:])
    except (ValueError, KeyError, TypeError):
        raise ApiError(401, 401, "求助凭证无效或已过期") from None
    try:
        row = await service.request_help(
            session,
            task_id=task_id,
            actor=actor,
            target_key=body.target_bot_key,
            question=body.question.strip(),
        )
        await session.commit()
    except ValueError as exc:
        raise ApiError(409, 409, str(exc)) from None
    return {
        "collaboration_id": str(row.id),
        "status": row.status,
        "instruction": (
            "求助已登记。立即结束本轮，简短告知人类正在等待伙伴反馈。"
            "不要轮询、不要猜测答案；反馈后平台会自动恢复你的原会话。"
        ),
    }
