"""`/api/me/assistant` 各路由文件的公共件：前缀、路由级依赖、「取本人的助理」、错误映射。

助理一律由登录人推出：这些接口没有任何「助理编号」参数，管理员也只能拿到自己的那一条。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import mcp_rpc
from coreman.api.deps import current_user, get_session, via_cli
from coreman.api.errors import ApiError
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.security import verify_csrf
from coreman.core.assistants import owner_view
from coreman.core.assistants.records import visible_to
from coreman.core.db.models import Bot, User

PREFIX = "/api/me/assistant"


async def no_store(response: Response) -> None:
    """个人内容不进任何缓存。"""
    response.headers.update(mcp_rpc.NO_STORE)


async def in_person(request: Request, _actor: User = Depends(current_user)) -> None:
    """管理命令以某位成员的身份建的会话不是本人在场：整个前缀都不受理。

    否则服务器上的人能以所有者的身份读他的知识库，还能导入置顶页——等于替他给助理写规则。
    依赖 `current_user`：会话是它认出来并放进 `request.state` 的。
    """
    if via_cli(request):
        raise ApiError(
            403, 403, "这些接口只对本人在管理台里的登录开放", [{"type": "cli_session_refused"}]
        )


# 路由级依赖：写操作先过 CSRF；成功的响应都带 no-store；管理命令的会话一律拒绝。
DEPENDENCIES = [Depends(verify_csrf), Depends(no_store), Depends(in_person)]


@dataclass(frozen=True)
class Mine:
    """登录人与他自己的助理记录。"""

    actor: User
    bot: Bot


async def mine(
    actor: User = Depends(interactive_user), session: AsyncSession = Depends(get_session)
) -> Mine:
    bot = await owner_view.current(session, actor.id)
    if bot is None or not visible_to(bot, actor):
        raise ApiError(404, 404, "你还没有个人助理", [{"type": "no_assistant"}])
    return Mine(actor, bot)


async def writable(own: Mine = Depends(mine)) -> Mine:
    """页面上的修改只在助理可用时受理：开通没走完、正在停用时改什么都没有意义。"""
    if own.bot.assistant_state != "active":
        raise ApiError(
            409,
            409,
            "个人助理还没开通好或正在停用，暂时不能修改",
            [{"type": "assistant_not_active"}],
        )
    return own


def refuse(code: str, table: Mapping[str, tuple[int, str]]) -> ApiError:
    """核心模块给的原因代码 → 接口错误：状态码与给人看的原话查表，代码放进 `errors[0].type`。"""
    status, message = table.get(code, (400, "这次操作没有完成"))
    return ApiError(status, status, message, [{"type": code}])
