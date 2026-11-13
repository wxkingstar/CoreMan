"""内容表（`sealed.COLUMNS` 里登记的模型）的读写入口台账。

个人助理的内容列在库里是密文。`session.get` / `session.scalar` 取整行会自动还原；
`session.scalars(select(<模型>))`、`session.execute(select(<模型>))`、只取内容列、裸 SQL 都不会，
写入也只在点名的几个文件里封存。这里按语法树扫 `coreman/` 下的全部代码，新增一处时会失败：
先决定这一处要不要 `sealed.open_rows` / `open_value`（读）或 `sealed.seal_values`（写），再更新
台账。各处的处理方式见 docs/superpowers/plans/2026-11-11-personal-assistant-1c-encryption.md
的「加解密所在的层」。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from coreman.core.assistants import sealed

ROOT = Path(__file__).resolve().parents[2]
# 模型名 → 登记的内容列。
CONTENT = {model.__name__: frozenset(columns) for model, columns in sealed.COLUMNS.items()}
TABLES = "|".join(sorted(str(model.__tablename__) for model in sealed.COLUMNS))  # type: ignore[attr-defined]
CONTENT_WORDS = re.compile(rf"\b(?:{'|'.join(sorted(set().union(*CONTENT.values())))})\b")
RAW_SQL = re.compile(rf"\b(?:UPDATE|INTO|FROM|JOIN)\s+(?:{TABLES})\b", re.IGNORECASE)

# 文件 → 取整行的 select 条数（实体出现在 select(...) 的任意位置）。
ENTITY_SELECTS = {
    "coreman/api/routers/chat_logs.py": 2,  # 只对所有者还原
    "coreman/api/routers/cron_jobs.py": 2,  # 只读状态
    "coreman/api/routers/dev.py": 1,  # 只对所有者还原
    "coreman/api/routers/infra_notify.py": 1,  # 不绑机器人的通知
    # 转录：上传时取整行（不取转录本身），session.scalar 自动还原
    "coreman/core/assistants/transcripts.py": 1,
    "coreman/api/routers/infra_push.py": 1,  # 显式还原
    "coreman/api/routers/runtime.py": 2,  # 运维列表，只出元数据
    "coreman/api/routers/self_reminders.py": 1,  # 只读状态
    "coreman/core/api_tasks/service.py": 1,  # session.scalar，自动还原
    # 审批单：去重、决定与平台自动撤回是 session.scalar（自动还原）；列表与清理附件显式还原；
    # 撤回与过期只改状态
    "coreman/core/assistants/approvals/service.py": 7,
    # 下班编译：被拒与已执行的审批单，显式还原后把摘要写进子任务的交代（密文列）
    "coreman/core/assistants/compiling.py": 1,
    # 投递：到点的条目、绑在一轮上的条目；都显式还原，读不出来的跳过
    "coreman/core/assistants/delivery.py": 2,
    # 待跟进：取一条是 session.scalar（自动还原）；列表显式还原
    "coreman/core/assistants/followups.py": 2,
    # 子任务：取一条、按结果任务号取一条都是 session.scalar（自动还原）；列表显式还原
    "coreman/core/assistants/jobs.py": 3,
    # 知识库：按路径摘要取一页、列出各页；都显式还原，读不出来的行报错或跳过
    "coreman/core/assistants/notes.py": 2,
    # 盯消息：认领一行是 session.scalar（自动还原上一轮的会话顺序与游标）
    "coreman/core/assistants/watch.py": 1,
    "coreman/core/bus/outbox.py": 1,  # 运维列表
    "coreman/core/bus/streams.py": 2,  # 显式还原，跳过不可读的行
    "coreman/core/bus/tasks.py": 3,  # 两处显式还原 + 运维列表
    "coreman/core/chat/bot_collaboration.py": 2,  # 群协作；只读状态与时间
    "coreman/core/cloud_proxy/decisions.py": 1,  # 云服务审批：session.scalar，自动还原
    "coreman/core/cloud_proxy/resume.py": 1,  # 定时来源的续跑：助理没有定时任务
    "coreman/core/cloud_proxy/service.py": 1,  # 只读状态与编号
    "coreman/core/chat/busy_choice.py": 1,  # 只读状态与时间
    "coreman/core/chat/topics.py": 1,  # 话题只在群里，助理不收群消息；session.scalar
    "coreman/core/chat/collaboration_setup.py": 1,  # 群协作
    "coreman/core/chat/human_collaboration.py": 2,  # session.scalar，自动还原
    "coreman/core/chat/interactions.py": 2,  # 交互状态：显式还原，读不出来的当作没有
    "coreman/core/chat/session_carry.py": 1,  # 显式还原
    "coreman/core/escalations/media_recovery.py": 1,  # session.scalar
    "coreman/core/escalations/service.py": 2,  # session.scalar；只读状态
    "coreman/core/knowledge/installation.py": 1,  # session.scalar
    "coreman/core/reminders.py": 1,  # 交互状态，session.scalar
    "coreman/runtime/gateway_common/inbound.py": 1,  # session.scalar
    "coreman/runtime/gateway_feishu/reply_buttons.py": 2,  # 都是 session.scalar
    "coreman/runtime/gateway_feishu/transport.py": 1,  # 显式还原，跳过不可读的行
    "coreman/runtime/scheduler/notifications.py": 1,  # 不绑机器人的通知
    "coreman/runtime/scheduler/reaper.py": 2,  # 显式还原
    "coreman/runtime/worker/chat/api.py": 1,  # session.scalar
    "coreman/runtime/worker/chat/feishu_quote.py": 2,  # session.scalar
    "coreman/runtime/worker/chat/queued_batch.py": 1,  # 显式还原
    "coreman/runtime/worker/chat/wecom_personal.py": 1,  # 交互状态，session.scalar
    "coreman/runtime/worker/choice_flow.py": 1,  # session.scalar
    "coreman/runtime/worker/cron_handler.py": 1,  # session.scalar
    "coreman/runtime/worker/escalation_media.py": 1,  # session.scalar
    "coreman/runtime/worker/group_write_cards.py": 1,  # 交互状态，session.scalar
    "coreman/runtime/worker/personal_cards.py": 1,  # 交互状态，session.scalar
    "coreman/runtime/worker/skill_install.py": 1,  # session.scalar
}
# 文件 → 语句里直接引用内容列（`<模型>.<内容列>`）的次数：过滤只能用留明文的键，取值要还原。
COLUMN_REFERENCES = {
    "coreman/api/routers/chat_logs.py": 2,  # 关键词 ILIKE：助理记录搜不到，接受
    "coreman/core/assistants/delivery_checks.py": 1,  # 盯消息的游标：取值后 open_value
    "coreman/core/assistants/notes.py": 1,  # 列索引时 defer 掉正文，不读它
    "coreman/core/assistants/transcripts.py": 1,  # 上传时 defer 掉旧的转录，不读它
    "coreman/core/bus/tasks.py": 3,  # 明文键 serialize_session、stale_hold
    "coreman/core/chat/busy_choice.py": 1,  # 明文键 platform_user_id
    "coreman/core/chat/group_observe.py": 2,  # 只处理群消息，助理不收群消息
    # patch_state：助理的行取值后 open_value 再整块封存；普通机器人的行在库里 `||` 合并
    "coreman/core/chat/interactions.py": 2,
    "coreman/core/chat/session_switch.py": 2,  # 取值后 open_value
    "coreman/core/chat/topics.py": 1,  # 话题挂起标记：话题只在群里，助理不收群消息
    "coreman/core/escalations/media_recovery.py": 2,  # 明文键 escalation_id、message_id
    "coreman/core/escalations/service.py": 1,  # 下划线开头的明文键
    "coreman/core/feishu_docs/access.py": 2,  # 明文键 doc_links（取回的封装顶层就有）
    "coreman/core/feishu_docs/consent.py": 1,  # 下划线开头的明文键
    "coreman/core/knowledge/installation.py": 1,  # 明文键 skill_id
    "coreman/core/personal_schedules.py": 1,  # 明文键 user_id
    "coreman/core/reminders.py": 1,  # 明文键 user_id
    "coreman/runtime/gateway_common/inbound.py": 7,  # 明文键
    "coreman/runtime/gateway_feishu/assistant_inbound.py": 1,  # 下划线开头的明文键
    "coreman/runtime/gateway_feishu/reply_buttons.py": 1,  # 下划线开头的明文键
    "coreman/runtime/scheduler/reaper.py": 1,  # 任务报错：取值后 open_value
    "coreman/runtime/worker/busy_choice.py": 1,  # 明文键 platform_user_id
    "coreman/runtime/worker/card_actions.py": 2,  # 明文键与 card.task_id 的明文镜像
    "coreman/runtime/worker/chat/feishu_quote.py": 4,  # 两处取值后 open_value，两处明文键过滤
    "coreman/runtime/worker/choice_flow.py": 1,  # card.task_id 的明文镜像
}
# 文件 → 提到内容列的裸 SQL 条数。
RAW_STATEMENTS = {
    "coreman/core/bus/tasks.py": 1,  # 认领：只读明文键
    "coreman/core/escalations/media_recovery.py": 1,  # 只读明文键
    "coreman/runtime/worker/chat/feishu_docs.py": 1,  # `payload || …` 追加明文键 doc_links
    "coreman/runtime/worker/credential_cards.py": 2,  # `#-` 之后对助理的行再经 ORM 删一次
}
# 文件 → insert / update / 构造这些模型的语句条数：内容列都经过 sealed.seal_values，或只写元数据。
# 在已登记的文件里新增一条写语句同样会失败。
WRITE_STATEMENTS = {
    "coreman/core/api_tasks/service.py": 1,  # 合成入站事件，载荷封存
    # 审批单：经 ORM 新建（flush 钩子封存）；另一条只写提醒时间
    "coreman/core/assistants/approvals/service.py": 2,
    # 投递：绑定任务号、记已交回、放回、作废，四条都只写元数据
    "coreman/core/assistants/delivery.py": 4,
    # 顺带检查：入提醒条目（摘要封存）；交回后被撤回的条目作废（只写状态）
    "coreman/core/assistants/delivery_checks.py": 2,
    # 待跟进：经 ORM 新建（flush 钩子封存）；另一条只把到期的推后条目改回未结
    "coreman/core/assistants/followups.py": 2,
    # 子任务：经 ORM 新建（flush 钩子封存）；之后的改动同样经 ORM
    "coreman/core/assistants/jobs.py": 1,
    # 知识库：经 ORM 新建（flush 钩子封存）；改写同样经 ORM
    "coreman/core/assistants/notes.py": 1,
    "coreman/core/assistants/settings.py": 1,  # 按默认值建行，不写内容列
    # 转录：派出前写交接编号、没到过节点的轮次清掉交接编号（都是元数据）；内容经 ORM 写入，
    # flush 钩子封存
    "coreman/core/assistants/transcripts.py": 2,
    # 盯消息：登记缺行的助理、意外失败后往后推，都只写调度用的列
    "coreman/core/assistants/watch.py": 2,
    # 收件箱：入箱经 seal_values 封存摘要；压着与两种作废只改级别、状态与时间
    "coreman/core/assistants/watch_inbox.py": 4,
    "coreman/core/bus/outbox.py": 8,  # add 封存载荷；其余只写状态与 last_error
    "coreman/core/bus/streams.py": 6,  # update_state / complete 封存；其余只写元数据
    "coreman/core/bus/tasks.py": 6,  # enqueue / finish 封存；其余只写状态
    # 交互状态：open_state 经 ORM 新建（flush 钩子封存）、patch_state 两条（助理的封存后写回）；
    # 其余只写状态
    "coreman/core/chat/interactions.py": 5,
    "coreman/core/chat/chat_logs.py": 5,  # 五处都封存
    # 云服务审批的挂起单：新建时没有理由；另一条只写状态。理由经 ORM 写入，flush 钩子封存
    "coreman/core/cloud_proxy/service.py": 2,
    "coreman/core/feishu_docs/confirm.py": 1,  # 群聊写入确认：经 ORM 新建，flush 钩子封存
    "coreman/runtime/gateway_common/inbound.py": 1,  # 入站事件，载荷封存
    # 已发消息的正文存档，封存；建投递行时没有内容（layout 之后经 ORM 改，由 flush 钩子封存）
    "coreman/runtime/gateway_feishu/transport.py": 2,
    "coreman/runtime/scheduler/reaper.py": 1,  # 结掉漏网的进行中记录，报错信息封存
    "coreman/runtime/worker/platform_turn.py": 1,  # 入站事件的替身：不加入会话、不落库
}


def _model(node: ast.expr) -> str | None:
    """`Task` 或 `models.Task` 这样的引用指向的模型名。"""
    name = (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else None
    )
    return name if name in CONTENT else None


def _called(node: ast.Call) -> str:
    func = node.func
    return (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else ""
    )


def _sources() -> Iterator[tuple[str, ast.AST]]:
    for path in sorted((ROOT / "coreman").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("coreman/core/db/models/"):
            continue
        yield relative, ast.parse(path.read_text(encoding="utf-8"))


def _is_entity_select(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _called(node) == "select"
        and any(_model(arg) for arg in node.args)
    )


def _is_column_reference(node: ast.AST) -> bool:
    if not isinstance(node, ast.Attribute):
        return False
    model = _model(node.value)
    return model is not None and node.attr in CONTENT[model]


def _is_raw_statement(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and RAW_SQL.search(node.value) is not None
        and CONTENT_WORDS.search(node.value) is not None
    )


def _is_write(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _called(node) in CONTENT:
        return True
    return (
        _called(node).endswith(("insert", "update"))
        and bool(node.args)
        and bool(_model(node.args[0]))
    )


def _count(matches: object) -> dict[str, int]:
    assert callable(matches)
    found: dict[str, int] = {}
    for relative, tree in _sources():
        count = sum(1 for node in ast.walk(tree) if matches(node))
        if count:
            found[relative] = count
    return found


def test_every_entity_select_is_reviewed() -> None:
    assert _count(_is_entity_select) == ENTITY_SELECTS


def test_every_content_column_reference_is_reviewed() -> None:
    assert _count(_is_column_reference) == COLUMN_REFERENCES


def test_every_raw_statement_on_content_is_reviewed() -> None:
    assert _count(_is_raw_statement) == RAW_STATEMENTS


def test_writes_stay_in_the_sealing_layer() -> None:
    assert _count(_is_write) == WRITE_STATEMENTS
