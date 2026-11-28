# 运行时全自动切换 · 第 0 期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉六处不依赖新机制的切换体验问题：额度告示被当成回答、换席位后账号对不上、来回切换丢上下文、回执与实际不符、切换中新消息没有提示、后台会话被静默关掉。

**Architecture:** 六个互不依赖的小改动，各自带测试。平台侧改 Python（worker、网关、领域逻辑），其中一项加一个只增不减的迁移；节点侧改守护进程（Python）和 Codex / OpenCode 驱动（Go）。不引入新概念，自动切换本身在第 2 期。

**Tech Stack:** Python 3 + SQLAlchemy（异步）+ Alembic + pytest；Go（`runtime_daemon/drivers`）；PostgreSQL。

**Spec:** `docs/superpowers/specs/2026-11-28-runtime-auto-switch.md`（§2 问题清单 5、6、8、9、10、13、16、17，§12 第 0 期）。

## Global Constraints

- 这是公开仓库。代码、注释、测试、提交信息里不出现任何公司名、内部域名、内部机器名、真实账号；示例一律用 `example.com`、`demo-bot`。
- 只做本计划列出的改动，不顺手改相邻代码、注释、格式。
- 提交信息用 `fix(<范围>): <中文说明>`，末尾带一行 `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`。
- 迁移只加列，开头保留 `op.execute("SET LOCAL lock_timeout = '5s'")`，旧版本代码不读新列也能跑。
- 建迁移前先 `ls migrations/versions | tail -3` 确认最新编号。本计划按最新是 `0138` 写；已被别的分支占用时顺延，并同步改 `revision` / `down_revision`。
- 文案键在 `coreman/core/i18n/messages.py` 的 `zh` 和 `ja` 两张表里都要加。
- 任务 2 的守护进程改动、任务 4 的驱动改动要升级运行时节点才生效；平台侧必须对没升级的节点照常工作。
- 检查命令：`uv run pytest <文件>`（需要 `TEST_DATABASE_URL`）、`uv run ruff check .`、`uv run mypy coreman`；驱动在 `runtime_daemon/drivers` 下跑 `go test ./...`。
- 分支：从 `main` 新开 `fix/runtime-switch-phase0`。

## File Structure

| 文件 | 任务 | 改什么 |
|---|---|---|
| `coreman/core/chat/rate_limit.py` | 1 | 加 `RATE_LIMITED_CODE` / `RATE_LIMITED_MESSAGE` 两个常量 |
| `coreman/runtime/worker/chat/classify.py` | 1 | 唯一的归类出口识别额度告示 |
| `coreman/runtime/worker/platform_turn.py` | 1 | 删掉重复的那段识别 |
| `coreman/runtime/worker/cron_handler.py` | 1 | 定时任务的额度告示按失败收尾 |
| `runtime_daemon/daemon.py` | 2 | 切换席位时立刻换成新席位的账号 |
| `coreman/api/routers/runtime_seats.py` | 2 | 席位清单写库时同步顶层账号 |
| `migrations/versions/0139_chat_session_lineage.py` | 3 | `chat_sessions.lineage` |
| `coreman/core/db/models/bus.py` | 3 | 模型加列 |
| `coreman/core/chat/sessions.py` | 3 | 记录、重置、返回沿革 |
| `coreman/core/chat/session_carry.py` | 3 | 续接文本按一组 relay 会话取 |
| `coreman/runtime/worker/chat/opening.py` | 3 | 把沿革传给续接 |
| `coreman/core/bots/switch_relay.py` | 4、6 | 结果里带「原样带过去几个」；关后台会话时带通知 |
| `coreman/runtime/worker/relay_switch.py` | 4 | 回执按实际情况写 |
| `runtime_daemon/drivers/cmd/relay-codex/{codex,stream}.go` | 4 | 带了续接文本时不说「会话记录已不存在」 |
| `runtime_daemon/drivers/cmd/relay-opencode/{opencode,stream}.go` | 4 | 同上 |
| `coreman/runtime/gateway_common/inbound.py` | 5 | 迁移中提示、不发忙碌卡 |
| `coreman/runtime/worker/chat/queued_batch.py` | 5 | 迁移中排队的消息不再问「还要处理吗」 |
| `coreman/core/chat/background_runs.py` | 6 | 关闭时可带通知 |
| `coreman/core/i18n/messages.py` | 4、5、6 | 新文案 |

---

### Task 1: 额度告示统一按「额度中断」归类

现在额度告示（如 `You've hit your limit · resets 3pm`）在普通对话里记成成功，定时任务把它当结果推给用户，任务会话可能据此判成「完成」。只有不出声的平台轮次（`platform_turn.py`）自己识别了一次。改成在唯一的归类出口识别。

**Files:**
- Modify: `coreman/core/chat/rate_limit.py`（`is_rate_limited` 之后）
- Modify: `coreman/runtime/worker/chat/classify.py:23-41`
- Modify: `coreman/runtime/worker/platform_turn.py:217-225`
- Modify: `coreman/runtime/worker/cron_handler.py:420-439`
- Test: `tests/integration/test_rate_limit_switch.py`、`tests/integration/test_cron_handler.py`

**Interfaces:**
- Produces: `coreman.core.chat.rate_limit.RATE_LIMITED_CODE: str = "rate_limited"`、`RATE_LIMITED_MESSAGE: str = "运行时额度用完"`。任何轮次的终稿是额度告示时，`Verdict.log_status == "error"`、`task_status == "failed"`、`error_code == RATE_LIMITED_CODE`。

- [ ] **Step 1: 写失败的测试（对话）**

加到 `tests/integration/test_rate_limit_switch.py` 末尾：

```python
async def test_rate_limited_turn_is_recorded_as_failed_not_as_an_answer(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import ChatLog, Task

    bot, relay, _ = await seed_bot(db_session)
    fake = FakeRelay("rate_limit")
    t = await chat_task(db_session, bot, "跑一下", sender="nobody")
    await run(db_engine, t, fake)
    task = await db_session.get(Task, t.id, populate_existing=True)
    assert task is not None and task.status == "failed" and task.error_code == "rate_limited"
    log = await db_session.scalar(select(ChatLog).where(ChatLog.bot_id == bot.id))
    assert log is not None and log.status == "error" and log.error_code == "rate_limited"
    s = await stream_of(db_session, t.id)
    # 不配「✅ 回复已生成」；额度表照旧附在后面。
    assert msg("done_suffix") not in s.final_text
    assert "**📊 服务器额度总览**" in s.final_text
```

- [ ] **Step 2: 写失败的测试（定时任务）**

加到 `tests/integration/test_cron_handler.py` 末尾：

```python
async def test_rate_limit_notice_fails_the_run_instead_of_being_pushed_as_the_result(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    await job(db_session, now, target_chats=["test-group"])
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    fake = FakeRelay("rate_limit")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await CronRunHandler().run(ctx)
    run = await db_session.scalar(select(CronRun))
    assert run is not None and run.status == "failed"
    assert (run.error_message or "").startswith("rate_limited")
    item = await db_session.scalar(select(OutboxItem))
    assert item is not None
    pushed = item.payload["markdown"]
    assert "运行时额度用完" in pushed and "hit your limit" not in pushed
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_rate_limit_switch.py::test_rate_limited_turn_is_recorded_as_failed_not_as_an_answer tests/integration/test_cron_handler.py::test_rate_limit_notice_fails_the_run_instead_of_being_pushed_as_the_result -v`
Expected: 两条都 FAIL（任务状态是 `succeeded`；定时任务状态是 `success`）。

- [ ] **Step 4: 加常量**

`coreman/core/chat/rate_limit.py`，在 `is_rate_limited` 函数之后：

```python
# 额度告示不是一轮该有的结果：各处收尾统一用这组错误码与说明。
RATE_LIMITED_CODE = "rate_limited"
RATE_LIMITED_MESSAGE = "运行时额度用完"
```

- [ ] **Step 5: 在归类出口识别**

`coreman/runtime/worker/chat/classify.py`。import 区加：

```python
from coreman.core.chat.rate_limit import (
    RATE_LIMITED_CODE,
    RATE_LIMITED_MESSAGE,
    is_rate_limited,
)
```

`_classify` 里，在 `verdict = self._classify_outcome(ctx, pre, out)` 之后、静默任务那段判断之前插入：

```python
        if verdict.log_status in ("success", "error") and is_rate_limited(verdict.final_text):
            # 终稿是「额度用完」的告示，不是这一轮该有的结果：按失败收尾，不配完成标记。
            # 驱动按正文回传（成功）和按出错回传的都走到这里，各类轮次不用各自再认一遍。
            verdict = Verdict(
                "error",
                "failed",
                RATE_LIMITED_CODE,
                RATE_LIMITED_MESSAGE,
                verdict.final_text.removesuffix(msg("done_suffix", ctx.locale)),
            )
```

- [ ] **Step 6: 删掉平台轮次里重复的识别**

`coreman/runtime/worker/platform_turn.py` 的 `_classify` 改成只剩提问那一段：

```python
    def _classify(self, ctx: TaskContext, pre: Prepared, out: Outcome) -> Verdict:
        verdict = super()._classify(ctx, pre, out)
        if verdict.log_status == "ask_user":
            # 没有人能答题：不开选择卡（它会挂在所有者的私聊上），题面留在终稿里。
            return replace(verdict, log_status="success")
        return verdict
```

同时删掉文件顶部不再使用的 `from coreman.core.chat.rate_limit import is_rate_limited`（先 `grep -n is_rate_limited coreman/runtime/worker/platform_turn.py` 确认没有别处用）。

- [ ] **Step 7: 定时任务按失败收尾**

`coreman/runtime/worker/cron_handler.py`。import 区加：

```python
from coreman.core.chat.rate_limit import (
    RATE_LIMITED_CODE,
    RATE_LIMITED_MESSAGE,
    is_rate_limited,
)
```

`_run` 里把 `except CronStreamError` 分支改成：

```python
        except CronStreamError as exc:
            status, error, detail = "failed", exc.code, exc.detail
            reply, usage, tools = exc.partial, exc.usage, exc.tools
            if is_rate_limited(f"{detail or ''}\n{reply or ''}"):
                # 驱动按出错回传的额度告示：推送里只说额度用完，不贴英文原文。
                error, detail = RATE_LIMITED_CODE, RATE_LIMITED_MESSAGE
```

并在 `reply, usage, tools, truncated = consume.result()` 之后紧接着加：

```python
                        if is_rate_limited(reply):
                            # 驱动按正文回传的额度告示：不是这次执行的结果。
                            raise CronStreamError(
                                RATE_LIMITED_CODE, RATE_LIMITED_MESSAGE, reply, usage, tools
                            )
```

- [ ] **Step 8: 跑测试确认通过，并跑相关回归**

Run: `uv run pytest tests/integration/test_rate_limit_switch.py tests/integration/test_cron_handler.py tests/integration/test_group_dispatch.py tests/integration/test_assistant_subtask_handler.py tests/integration/test_assistant_bot_help_handler.py tests/unit/test_rate_limit.py -v`
Expected: 全部 PASS。已有用例里断言「撞限轮次状态为成功」的，改成新行为（`failed` / `rate_limited`）；其余断言不动。

- [ ] **Step 9: Commit**

```bash
git add coreman/core/chat/rate_limit.py coreman/runtime/worker/chat/classify.py coreman/runtime/worker/platform_turn.py coreman/runtime/worker/cron_handler.py tests/integration/test_rate_limit_switch.py tests/integration/test_cron_handler.py
git commit -m "fix(worker): 额度告示统一按额度中断收尾，不再当成回答"
```

---

### Task 2: 换席位后账号立刻换成新席位的

节点切换席位后，返回的席位清单里「当前席位」的账号取的是切换前缓存的旧账号，要等下一次发现（约 60 秒）才更正。这期间管理台显示错的账号，平台也会按旧账号把别的节点上报的额度写到这个实例上。

**Files:**
- Modify: `runtime_daemon/daemon.py:524-541`
- Modify: `coreman/api/routers/runtime_seats.py:91-103`
- Test: `tests/unit/test_runtime_seats.py`、`tests/api/test_runtime_seats.py`

**Interfaces:**
- Produces: 节点 `seat-activate` 返回的 `seats` 里，`active` 那一项的 `account` / `login` 是新席位的；平台 `node.capabilities[provider]["account"]` / `["login"]` 与当前席位一致。

- [ ] **Step 1: 写失败的测试（节点）**

`tests/unit/test_runtime_seats.py` 的 `test_activating_a_seat_switches_the_driver_and_reports_seats` 里，在 `assert [s["active"] for s in switched["seats"]] == [False, True]` 之后加：

```python
    # 当前席位的账号立刻是新席位的，不用等下一次发现。
    seat_dir = str(daemon.seats.directory("claude", seat))
    assert switched["seats"][1]["account"] == seat_dir
    assert switched["seats"][0]["account"] == "default"
    assert daemon.capabilities["claude"]["account"] == seat_dir
```

（这个用例的 `fake_status` 用配置目录当账号名：默认席位是 `"default"`，新席位是它的目录路径。）

- [ ] **Step 2: 写失败的测试（平台）**

`tests/api/test_runtime_seats.py` 的 `test_admin_adds_signs_in_and_switches_a_seat` 里，在调用 `.../activate` 并断言成功之后加：

```python
    await db_session.refresh(node)
    assert node.capabilities["claude"]["account"] == "b@example.com"
    assert node.capabilities["claude"]["login"] == "ready"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_runtime_seats.py::test_activating_a_seat_switches_the_driver_and_reports_seats tests/api/test_runtime_seats.py::test_admin_adds_signs_in_and_switches_a_seat -v`
Expected: 两条都 FAIL（节点返回的仍是 `a@example.com`；平台顶层没有 `account`）。

- [ ] **Step 4: 节点切换时用刚查到的状态**

`runtime_daemon/daemon.py` 的 `seat_operation`，把 `seat-activate` 分支里从登录检查到 `rotate_driver` 的部分改成：

```python
            status = cli_status(provider, seats.env(provider, seat_id))
            if status["login"] != "ready":
                raise OperationError("该席位未登录，请先登录")
            with self.daemon.driver_lock:
                seats.activate(provider, seat_id)
                cap = self.daemon.capabilities[provider]
                for key in ("plan_type", "account_verified"):
                    cap.pop(key, None)
                # 当前席位的账号与登录状态立刻换成新席位的：等下一次发现要一分钟，这期间管理台
                # 显示的是旧账号，平台也会按旧账号处理额度。
                cap["account"], cap["login"] = status.get("account", ""), status["login"]
                for key in ("plan_type", "account_verified"):
                    if key in status:
                        cap[key] = status[key]
                self.daemon.rotate_driver(provider)
```

- [ ] **Step 5: 平台写席位清单时同步顶层账号**

`coreman/api/routers/runtime_seats.py` 的 `store_seats`，把 `if node is not None:` 那一段改成：

```python
    if node is not None:
        caps = dict(node.capabilities or {})
        entry = {**(caps.get(provider) or {}), "seats": states}
        active = next((s for s in states if s.get("active")), None)
        if active is not None:
            # 节点列表显示的账号取顶层这两项：跟着当前席位立刻换，不等下一次心跳。
            entry["account"], entry["login"] = active.get("account", ""), active.get("login")
        caps[provider] = entry
        node.capabilities = caps
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_runtime_seats.py tests/api/test_runtime_seats.py tests/api/test_admin_cli_runtimes.py -v`
Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add runtime_daemon/daemon.py coreman/api/routers/runtime_seats.py tests/unit/test_runtime_seats.py tests/api/test_runtime_seats.py
git commit -m "fix(runtime): 换席位后账号与登录状态立刻换成新席位的"
```

---

### Task 3: 续接文本按会话沿革取

换运行时带不走原生会话时，新会话首轮会附上原会话最近的对话。现在只取上一个 relay 会话的记录，所以 A → B 聊两轮再 → A 之后，回来只带那两轮。给 `chat_sessions` 记下这个会话键换过的 relay 会话，续接时一起取。

**Files:**
- Create: `migrations/versions/0139_chat_session_lineage.py`
- Modify: `coreman/core/db/models/bus.py`（`ChatSession`，`inherited_from` 之后）
- Modify: `coreman/core/chat/sessions.py`（`SessionInfo`、`get_or_create`、`switch_to`、`carry_over`）
- Modify: `coreman/core/chat/session_carry.py:33-54`
- Modify: `coreman/runtime/worker/chat/opening.py:523-549`
- Test: `tests/integration/test_chat_sessions.py`

**Interfaces:**
- Produces: `ChatSession.lineage: list[dict[str, str]]`，每项 `{"id": <relay 会话 id>, "backend": <底座>}`，从旧到新，最多 10 项；会话换新（过期、换底座后不再续用、`switch_to`）时清空。
- Produces: `SessionInfo.lineage: tuple[uuid.UUID, ...]`。
- Produces: `carried_context(session, *, bot_id, session_key, relay_session_ids: Sequence[uuid.UUID], header=HEADER) -> str | None`。参数名从 `relay_session_id` 改成 `relay_session_ids`。

- [ ] **Step 1: 写失败的测试**

加到 `tests/integration/test_chat_sessions.py` 末尾（`_bot` 是这个文件里已有的助手）：

```python
async def test_round_trip_switch_still_carries_what_was_said_before_the_first_switch(
    db_session: AsyncSession,
) -> None:
    from coreman.core.chat.session_carry import carried_context
    from coreman.core.db.models import ChatLog, ChatSession

    bot = await _bot(db_session)

    async def turn(backend: str):  # type: ignore[no-untyped-def]
        return await sessions.get_or_create(
            db_session,
            bot_id=bot.id,
            session_key="a",
            backend=backend,
            ttl_hours=72,
            speaker_user_id=None,
        )

    def log(sid: uuid.UUID, text: str, reply: str, at: datetime) -> None:
        db_session.add(
            ChatLog(
                bot_id=bot.id,
                bot_key=bot.bot_key,
                platform="wecom",
                chat_type="single",
                session_key="a",
                relay_session_id=sid,
                user_name="张三",
                message_type="text",
                status="success",
                request_at=at,
                message_content=text,
                response_content=reply,
            )
        )

    now = datetime.now(UTC)
    first = await turn("claude")
    log(first.relay_session_id, "最早的问题", "最早的回答", now)
    # 第一次切换：换到另一种底座，原生会话带不过去。
    await sessions.carry_over(db_session, bot.id, backend="codex", keep=set())
    second = await turn("codex")
    assert second.relay_session_id != first.relay_session_id
    assert second.lineage == (first.relay_session_id,)
    log(second.relay_session_id, "中间的问题", "中间的回答", now + timedelta(seconds=1))
    await sessions.settle_carry(
        db_session, bot_id=bot.id, session_key="a", relay_session_id=second.relay_session_id
    )
    # 第二次切换：换回来。
    await sessions.carry_over(db_session, bot.id, backend="claude", keep=set())
    third = await turn("claude")
    assert third.lineage == (first.relay_session_id, second.relay_session_id)
    note = await carried_context(
        db_session,
        bot_id=bot.id,
        session_key="a",
        relay_session_ids=[*third.lineage, third.inherited_from],
    )
    assert note is not None
    assert note.index("最早的问题") < note.index("中间的问题")
    # 会话过期换新之后，沿革清空：之前的内容不再跟过来。
    row = await db_session.get(ChatSession, (bot.id, "a"), populate_existing=True)
    assert row is not None
    row.last_active_at = now - timedelta(hours=100)
    await db_session.flush()
    fresh = await turn("claude")
    assert fresh.is_new and fresh.lineage == () and fresh.inherited_from is None
```

文件顶部缺 `uuid`、`timedelta` 的 import 时补上。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_chat_sessions.py::test_round_trip_switch_still_carries_what_was_said_before_the_first_switch -v`
Expected: FAIL，`SessionInfo` 没有 `lineage`。

- [ ] **Step 3: 迁移**

新建 `migrations/versions/0139_chat_session_lineage.py`：

```python
"""`chat_sessions.lineage`: the relay sessions a session key went through since it last started
fresh, oldest first, as `{"id", "backend"}`.

After a runtime switch that could not carry the native session, the first turn gets the recent
conversation as text. It used to read only the previous relay session, so switching away and
back lost everything said before the first switch. Additive only: the previous version never
reads the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "chat_sessions",
        sa.Column(
            "lineage",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "lineage")
```

- [ ] **Step 4: 模型加列**

`coreman/core/db/models/bus.py` 的 `ChatSession`，在 `inherited_from` 那一行之后：

```python
    # 这个会话键从上次换新以来用过的 relay 会话（旧 → 新，{"id", "backend"}）：换运行时带不走
    # 原生会话时，续接文本按它取全，来回切换不丢最初那一段。
    lineage: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
```

- [ ] **Step 5: 会话读写带上沿革**

`coreman/core/chat/sessions.py`。

`SessionInfo` 末尾加一个字段：

```python
    # 这个会话键从上次换新以来用过的 relay 会话（旧 → 新）：续接文本按它取全。
    lineage: tuple[uuid.UUID, ...] = ()
```

文件里（`SessionInfo` 之后）加上限常量：

```python
# 沿革最多记这么多项：再往前的对话早已超出续接文本的长度上限。
LINEAGE_MAX = 10
```

`get_or_create` 里，在 `forked = ...` 之后加：

```python
    # 换了新会话（过期、换底座）就不再有沿革。
    lineage = list(row.lineage or []) if (fresh and row is not None) else []
```

同一函数的 `insert(ChatSession).values(...)` 加 `lineage=lineage,`，`on_conflict_do_update` 的 `set_` 里加 `"lineage": lineage,`，返回值改成：

```python
    return SessionInfo(
        sid,
        not fresh,
        speaker_changed,
        previous,
        inherited,
        forked,
        tuple(uuid.UUID(item["id"]) for item in lineage),
    )
```

`switch_to` 的 `on_conflict_do_update` 的 `set_` 里加一项（指向历史会话或换新时沿革作废）：

```python
                "lineage": [],
```

`carry_over` 的循环体改成：

```python
    for row in await list_for_bot(session, bot_id):
        row.inherited_from = row.inherited_from or row.relay_session_id
        if row.relay_session_id in keep and row.backend == backend:
            continue
        # 带不过去的换新会话，原来那条记进沿革：下次再换时续接文本还能取到它。
        entry = {"id": str(row.relay_session_id), "backend": row.backend}
        earlier = [item for item in (row.lineage or []) if item.get("id") != entry["id"]]
        row.lineage = [*earlier, entry][-LINEAGE_MAX:]
        row.relay_session_id = uuid.uuid4()
        row.backend = backend
```

- [ ] **Step 6: 续接文本按一组会话取**

`coreman/core/chat/session_carry.py`。文件顶部 import 加 `from collections.abc import Sequence`，`carried_context` 的签名与查询改成：

```python
async def carried_context(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    session_key: str,
    relay_session_ids: Sequence[uuid.UUID],
    header: str = HEADER,
) -> str | None:
    rows = (
        await session.scalars(
            select(ChatLog)
            .where(
                ChatLog.bot_id == bot_id,
                ChatLog.session_key == session_key,
                ChatLog.relay_session_id.in_(list(relay_session_ids)),
                ChatLog.status.in_(("success", "ask_user")),
            )
            .order_by(ChatLog.request_at.desc())
            .limit(MAX_TURNS)
        )
    ).all()
```

函数其余部分不动。文件开头的模块说明里「按时间顺序排」前补半句：`同一个会话键来回换过几次运行时的，沿革里的几个 relay 会话一起取。`

- [ ] **Step 7: 调用方传沿革**

`coreman/runtime/worker/chat/opening.py`，续接那段改成：

```python
            carried = await carried_context(
                session,
                bot_id=bot.id,
                session_key=intake.session_key,
                # 沿革在前：来回换过运行时的，最初那一段也在里面。
                relay_session_ids=list(dict.fromkeys([*info.lineage, info.inherited_from])),
            )
```

分叉那段只把参数名改掉：

```python
                relay_session_ids=[info.forked_from],
```

然后 `grep -rn "relay_session_id=" coreman tests | grep carried_context` 以及 `grep -rn "carried_context(" coreman tests`，把其余调用处（含 `tests/integration/test_chat_sessions.py` 里已有的那一处）一并改成 `relay_session_ids=[...]`。

- [ ] **Step 8: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_chat_sessions.py tests/integration/test_chat_handler.py tests/api/test_workspace_migration.py tests/integration/test_assistant_transcript_carry.py -v`
Expected: 全部 PASS。

- [ ] **Step 9: Commit**

```bash
git add migrations/versions/0139_chat_session_lineage.py coreman/core/db/models/bus.py coreman/core/chat/sessions.py coreman/core/chat/session_carry.py coreman/runtime/worker/chat/opening.py tests/integration/test_chat_sessions.py
git commit -m "fix(chat): 换运行时的续接文本按会话沿革取，来回切换不丢最初那一段"
```

---

### Task 4: 回执与驱动提示如实说明上下文

切换成功的回执不管会话有没有原样带过去，都写「对话上下文已随运行时带过去」。Codex、OpenCode 驱动续不上原会话时，即使平台给了续接文本，也在回答前加一句「之前的会话记录已不存在，已开启新会话」。

**Files:**
- Modify: `coreman/core/bots/switch_relay.py:60-67, 259-266, 294-296`
- Modify: `coreman/runtime/worker/relay_switch.py:112-118`
- Modify: `coreman/core/i18n/messages.py`（`relay_switch_ok`，zh 与 ja）
- Modify: `runtime_daemon/drivers/cmd/relay-codex/codex.go`（`codexInput`、`newRebuildFresh`）、`stream.go:387, 617`
- Modify: `runtime_daemon/drivers/cmd/relay-opencode/opencode.go`（输入结构体、`newRebuildFresh`）、`stream.go:416, 587`
- Test: `tests/integration/test_rate_limit_switch.py`、`runtime_daemon/drivers/cmd/relay-codex/resume_fallback_test.go`、`runtime_daemon/drivers/cmd/relay-opencode/stream_test.go`

**Interfaces:**
- Produces: `SwitchResult.sessions_total: int`（切换时这个员工的会话数）、`SwitchResult.sessions_native: int`（其中原生会话原样续用的）。
- Produces: 文案键 `relay_switch_context_kept`、`relay_switch_context_partial`（参数 `n`）；`relay_switch_ok` 多一个参数 `context`。

- [ ] **Step 1: 写失败的测试（回执）**

`tests/integration/test_rate_limit_switch.py` 里已有的 `test_relay_switch_handler_switches_and_notifies` 正好是「会话没能带过去」的情况（这个文件的自动夹具让 `prepare_switch` 返回空集合）。把它对回执的断言改成带上新参数：

```python
    assert items[-1].payload["markdown"] == msg(
        "relay_switch_ok",
        current="r1",
        target="r-idle",
        detail=msg("relay_switch_detail", model="claude-sonnet-5-5"),
        context=msg("relay_switch_context_partial", n=1),
    )
```

再在它后面加一条「原样带过去」的用例：

```python
async def test_receipt_says_context_was_kept_when_the_native_session_moved(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, relay, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    idle = await _second_relay(db_session)
    creator = await _creator_identity(db_session, bot)
    before = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="zs",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=creator.id,
    )

    async def moved(*_args: object, **_kwargs: object) -> tuple[str, bool, set]:
        return "not_configured", False, {before.relay_session_id}

    monkeypatch.setattr(switch_relay, "prepare_switch", moved)
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={
            "idx": 0,
            "platform_user_id": "zs",
            "user_id": str(creator.id),
            "current_relay_id": str(relay.id),
            "current_name": "r1",
            "target_relay_id": str(idle.id),
            "target_name": "r-idle",
            "target_pct_7d": 5.0,
            "chat_id": "zs",
            "chat_type": "single",
        },
        task_id_prefix="ratelimit_switch@sales_bot@zs@1",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    await interactions.set_status(db_session, st.id, "submitted")
    await db_session.commit()
    t = await _switch_task(db_session, bot, st, user_id=creator.id, target_id=idle.id)
    await RelaySwitchHandler().run(build_ctx(db_engine, t))
    (row,) = await sessions.list_for_bot(db_session, bot.id)
    await db_session.refresh(row)
    assert row.relay_session_id == before.relay_session_id
    items = (await db_session.execute(select(OutboxItem))).scalars().all()
    text = items[-1].payload["markdown"]
    assert msg("relay_switch_context_kept") in text
    assert "没能原样带过去" not in text
```

- [ ] **Step 2: 写失败的测试（驱动）**

`runtime_daemon/drivers/cmd/relay-codex/resume_fallback_test.go` 的 `TestStaleThreadRetryCarriesResumeFallback` 里，在检查 `hello again` 之后加：

```go
	if strings.Contains(rec.Body.String(), "之前的会话记录已不存在") {
		t.Errorf("carried context must not be announced as lost:\n%s", rec.Body.String())
	}
```

同一文件末尾加一条没有续接文本的对照：

```go
// Without a text continuation the conversation really starts over, and the
// answer says so.
func TestStaleThreadRetryWithoutFallbackSaysTheRecordIsGone(t *testing.T) {
	dir := fakeCodex(t, `for a in "$@"; do
  if [ "$a" = resume ]; then /bin/cat > /dev/null; echo 'Error: no rollout found for thread id dead-thread' >&2; exit 1; fi
done
/bin/cat > /dev/null
echo '{"type":"thread.started","thread_id":"fresh-thread"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"hello again"}}'
echo '{"type":"turn.completed"}'
`)
	withSessionState(t)
	if err := threads.Set("s1", "dead-thread"); err != nil {
		t.Fatal(err)
	}
	req := &openai.ChatCompletionRequest{
		SessionID: "s1",
		Messages:  []openai.ChatMessage{{Role: "user", Content: json.RawMessage(`"hi"`)}},
	}
	input := buildCodexInput(req, "m", threads.Get("s1"), "")
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "m", false, dir, nil, "s1", newRebuildFresh(threads, req, "m", ""))
	if body := rec.Body.String(); !strings.Contains(body, "之前的会话记录已不存在") || !strings.Contains(body, "hello again") {
		t.Fatalf("body:\n%s", body)
	}
}
```

`runtime_daemon/drivers/cmd/relay-opencode/stream_test.go` 的 `TestMissingSessionStartsFreshWithFallback`，把

```go
	if !strings.Contains(body, "之前的会话记录已不存在") || !strings.Contains(body, "hello again") {
		t.Fatal(body)
	}
```

改成

```go
	// The request carried a text continuation: the context is not lost, so no notice.
	if strings.Contains(body, "之前的会话记录已不存在") || !strings.Contains(body, "hello again") {
		t.Fatal(body)
	}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_rate_limit_switch.py::test_relay_switch_handler_switches_and_notifies tests/integration/test_rate_limit_switch.py::test_receipt_says_context_was_kept_when_the_native_session_moved -v`
Expected: 两条都 FAIL，没有 `relay_switch_context_partial` / `relay_switch_context_kept` 这两个文案键。

Run（在 `runtime_daemon/drivers` 下）: `go test ./cmd/relay-codex/ ./cmd/relay-opencode/ -run 'TestStaleThreadRetry|TestMissingSessionStartsFreshWithFallback'`
Expected: `TestStaleThreadRetryCarriesResumeFallback` 与 `TestMissingSessionStartsFreshWithFallback` FAIL；新加的对照用例 PASS。

- [ ] **Step 4: 切换结果带上会话数**

`coreman/core/bots/switch_relay.py`。`SwitchResult` 末尾加两个字段：

```python
    # 切换时这个员工的会话数，以及其中原生会话原样续用的：回执据此如实说明上下文。
    sessions_total: int = 0
    sessions_native: int = 0
```

`switch_relay` 里，把

```python
    await sessions.carry_over(
        session, bot.id, backend=backend_of(new_model, target.model_provider), keep=carried
    )
```

改成

```python
    new_backend = backend_of(new_model, target.model_provider)
    rows = await sessions.list_for_bot(session, bot.id)
    native = sum(1 for row in rows if row.relay_session_id in carried and row.backend == new_backend)
    await sessions.carry_over(session, bot.id, backend=new_backend, keep=carried)
```

末尾的返回改成：

```python
    return SwitchResult(
        old_relay_id,
        target.id,
        old_model,
        new_model,
        bot.effort_level,
        memory_status,
        sessions_total=len(rows),
        sessions_native=native,
    )
```

- [ ] **Step 5: 文案**

`coreman/core/i18n/messages.py`。`zh` 表里把 `relay_switch_ok` 的最后一行改成带 `{context}`，并在它后面加两条：

```python
        "relay_switch_ok": (
            "✅ **已切换运行时**\n\n"
            "- 来源：{current}\n"
            "- 目标：{target}\n"
            "- 详情：{detail}\n\n"
            "_提示：{context}刚才因额度中断的那条请重新发送一次。_"
        ),
        "relay_switch_context_kept": "对话上下文已原样带过去，接着聊就行；",
        "relay_switch_context_partial": (
            "有 {n} 个对话没能原样带过去，已按最近的对话记录接上，较早的细节可能要再说一次；"
        ),
```

`ja` 表里对应的 `relay_switch_ok` 同样把说明上下文的那半句换成 `{context}`（其余文字不动），并加：

```python
        "relay_switch_context_kept": "会話の文脈はそのまま引き継がれました。続けて話せます。",
        "relay_switch_context_partial": (
            "{n} 件の会話はそのまま引き継げなかったため、直近のやり取りの記録で続きを補いました。"
            "古い内容はもう一度伝える必要があるかもしれません。"
        ),
```

- [ ] **Step 6: 回执按实际情况写**

`coreman/runtime/worker/relay_switch.py`，成功分支里把 `text = msg("relay_switch_ok", ...)` 改成：

```python
                lost = result.sessions_total - result.sessions_native
                text = msg(
                    "relay_switch_ok",
                    ctx.locale,
                    current=current,
                    target=target_name,
                    detail=msg("relay_switch_detail", ctx.locale, model=result.new_model),
                    context=(
                        msg("relay_switch_context_partial", ctx.locale, n=lost)
                        if lost > 0
                        else msg("relay_switch_context_kept", ctx.locale)
                    ),
                )
```

- [ ] **Step 7: Codex 驱动带了续接文本时不提示**

`runtime_daemon/drivers/cmd/relay-codex/codex.go`。`codexInput` 结构体加一个字段：

```go
	// Carried is set on a rebuilt input whose request brought a text
	// continuation: the earlier context reaches the new thread, so the answer
	// must not claim the conversation record is gone.
	Carried bool
```

`newRebuildFresh` 返回的闭包里，在 `input.Stdin = withResumeFallback(input.Stdin, req.ResumeFallback)` 之后加：

```go
		input.Carried = req.ResumeFallback != ""
```

`stream.go` 两处：流式的

```go
	if rebuilt {
```

改成

```go
	if rebuilt && !input.Carried {
```

非流式的

```go
	if rebuilt {
		body = staleThreadNotice + body
```

同样改成 `if rebuilt && !input.Carried {`。两处用的都是当前函数里被 `input = rebuildFresh()` 重新赋值过的 `input`。

- [ ] **Step 8: OpenCode 驱动同样处理**

`runtime_daemon/drivers/cmd/relay-opencode/opencode.go`。`opencodeInput` 结构体加一个字段：

```go
	// Carried is set on a rebuilt input whose request brought a text
	// continuation: the earlier context reaches the new session, so the answer
	// must not claim the conversation record is gone.
	Carried bool
```

`newRebuildFresh` 的闭包里，在 `input.Stdin = withResumeFallback(input.Stdin, req.ResumeFallback)` 之后加：

```go
		input.Carried = req.ResumeFallback != ""
```

`stream.go` 两处：流式的

```go
	if rebuilt {
		textDelta(staleSessionNotice)
```

和非流式的

```go
	if rebuilt {
		body = staleSessionNotice + body
```

都把条件改成 `if rebuilt && !input.Carried {`。

- [ ] **Step 9: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_rate_limit_switch.py tests/integration/test_switch_jobs.py -v`
Expected: 全部 PASS。已有用例里断言回执全文的，按新文案改期望值。

Run（在 `runtime_daemon/drivers` 下）: `go test ./...`
Expected: 全部 PASS。

- [ ] **Step 10: Commit**

```bash
git add coreman/core/bots/switch_relay.py coreman/runtime/worker/relay_switch.py coreman/core/i18n/messages.py tests/integration/test_rate_limit_switch.py runtime_daemon/drivers/cmd/relay-codex runtime_daemon/drivers/cmd/relay-opencode
git commit -m "fix(switch): 切换回执与驱动提示按上下文实际带过去的情况说明"
```

---

### Task 5: 切换中的新消息给提示，不弹忙碌卡

员工在换运行时期间不认领任务。这时用户发的消息只是入队，没有任何提示；同一人发第二条会弹「上一条还在排队，还没开始」的忙碌卡；切完后等了 10 分钟以上的还会被问「还要处理吗」。

**Files:**
- Modify: `coreman/runtime/gateway_common/inbound.py`（`_enqueue_task`、`_queue_behind_others`，新增 `_migrating`、`_notice_migrating`）
- Modify: `coreman/runtime/worker/chat/queued_batch.py`（`hold_if_stale`）
- Modify: `coreman/core/i18n/messages.py`
- Test: `tests/integration/test_busy_choice.py`

**Interfaces:**
- Produces: 迁移期间入队的对话任务 `payload["queued_migrating"] is True`；文案键 `switch_in_progress`；出站项去重键 `migrating:<bot_id>:<操作号>:<会话键>`。

- [ ] **Step 1: 写失败的测试**

加到 `tests/integration/test_busy_choice.py` 末尾：

```python
async def test_messages_sent_while_switching_runtimes_get_one_notice_and_no_busy_card(
    db_session: AsyncSession,
) -> None:
    bot, _, _ = await seed_bot(db_session)
    bot.workspace_state, bot.workspace_operation_id = "migrating", uuid.uuid4()
    await db_session.commit()
    first = await _say(db_session, bot, "第一条")
    second = await _say(db_session, bot, "第二条")
    items = (await db_session.scalars(select(OutboxItem).order_by(OutboxItem.id))).all()
    # 这个会话只提示一次；没有「上一条还在处理」的卡片。
    notices = [i for i in items if i.dedupe_key.startswith("migrating:")]
    assert [n.payload["markdown"] for n in notices] == [msg("switch_in_progress")]
    assert all("card" not in i.payload for i in items)
    for task in (first, second):
        await db_session.refresh(task)
        assert task.payload["queued_migrating"] is True
        assert "busy_offer" not in task.payload
    # 第二条仍排在第一条后面按顺序处理。
    assert second.payload.get("serialize_session") is True


async def test_messages_queued_during_a_switch_are_not_asked_about_again(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.runtime.worker.chat import queued_batch

    bot, _, _ = await seed_bot(db_session)
    bot.workspace_state, bot.workspace_operation_id = "migrating", uuid.uuid4()
    await db_session.commit()
    await _say(db_session, bot, "第一条")
    head = await _say(db_session, bot, "第二条")
    assert head.payload.get("serialize_session") is True
    ctx = build_ctx(db_engine, head)
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    held = await queued_batch.hold_if_stale(
        db_session,
        ctx,
        SimpleNamespace(bot=bot, chat_id="zs", inbound=SimpleNamespace(reply_context={})),  # type: ignore[arg-type]
        head,
        [queued_batch.Entry(text="第二条", at=long_ago)],
    )
    assert held is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_busy_choice.py::test_messages_sent_while_switching_runtimes_get_one_notice_and_no_busy_card tests/integration/test_busy_choice.py::test_messages_queued_during_a_switch_are_not_asked_about_again -v`
Expected: 第一条 FAIL（没有 `switch_in_progress` 文案键）；第二条 FAIL（`held` 是 `True`，或者在往下走到发确认卡时因为替身对象缺字段而报错——两种都说明没有提前返回）。

- [ ] **Step 3: 文案**

`coreman/core/i18n/messages.py`，`zh` 表里 `group_queued` 之后：

```python
        "switch_in_progress": "正在换运行时，换好后自动处理这条消息，不用重发。",
```

`ja` 表里 `group_queued` 之后：

```python
        "switch_in_progress": (
            "ランタイムを切り替え中です。完了後にこのメッセージを自動で処理しますので、再送は不要です。"
        ),
```

- [ ] **Step 4: 网关识别迁移并提示**

`coreman/runtime/gateway_common/inbound.py`。在 `_enqueue_task` 之前加两个函数：

```python
async def _migrating(session: AsyncSession, bot_id: uuid.UUID) -> uuid.UUID | None:
    """员工正在换运行时：返回这次迁移的操作号（提示按它去重），不在迁移中返回 None。"""
    from sqlalchemy import select

    from coreman.core.db.models import Bot

    row = (
        await session.execute(
            select(Bot.workspace_state, Bot.workspace_operation_id).where(Bot.id == bot_id)
        )
    ).one_or_none()
    if row is None or row.workspace_state != "migrating":
        return None
    return row.workspace_operation_id or uuid.UUID(int=0)


async def _notice_migrating(
    session: AsyncSession,
    bot: InboundBot,
    message: InboundMessage,
    session_key: str | None,
    migration: uuid.UUID,
) -> None:
    """换运行时期间来的消息：每个会话每次迁移只说一次「换好后自动处理」。"""
    target: dict[str, Any] = {"chat_id": message.chat_id}
    payload: dict[str, Any] = {"markdown": msg("switch_in_progress")}
    message_id = message.reply_context.get("message_id")
    if message.platform == "feishu" and message_id:
        target["message_id"] = message_id
        payload["_reply"] = True
    await outbox.add(
        session,
        bot_id=bot.id,
        platform=message.platform,
        kind="send",
        dedupe_key=f"migrating:{bot.id}:{migration}:{session_key}",
        target=target,
        payload=payload,
    )
```

`_enqueue_task` 里，在 `background_offer = (...)` 之前加：

```python
    migration = (
        await _migrating(session, bot.id) if new.kind == "chat" and not helper_reply else None
    )
    if migration is not None:
        # 等的是换运行时：收尾时不按「等太久」再问一次要不要处理。
        new.payload["queued_migrating"] = True
```

把调用 `_queue_behind_others` 的那一行改成：

```python
        await _queue_behind_others(session, bot, message, new, event_id, quiet=migration is not None)
```

把 `task = await enqueue(session, new)` 之前的 `offer = (...)` 之后加：

```python
    if migration is not None and offer:
        # 上一条没开始是因为在换运行时：不发「上一条还在处理」的卡，也不占发卡的名额。
        new.payload.pop("busy_offer", None)
        offer = False
```

在 `if task is not None and offer:` 之前加：

```python
    if task is not None and migration is not None:
        await _notice_migrating(session, bot, message, new.session_key, migration)
```

`_queue_behind_others` 的签名加一个仅限关键字的参数，并在发「先做完手上的」那句之前判断：

```python
async def _queue_behind_others(
    session: AsyncSession,
    bot: InboundBot,
    message: InboundMessage,
    new: NewTask,
    event_id: int,
    *,
    quiet: bool = False,
) -> None:
```

```python
    new.payload["serialize_session"] = True
    message_id = message.reply_context.get("message_id")
    # 换运行时期间另有一句提示，这句「先做完手上的」不再发。
    if not quiet and message.platform == "feishu" and message_id:
```

- [ ] **Step 5: 迁移中排队的不再问「还要处理吗」**

`coreman/runtime/worker/chat/queued_batch.py` 的 `hold_if_stale`，开头的条件加一项：

```python
    if (
        not p.get("serialize_session")
        or p.get("stale_confirmed")
        or p.get("busy_wait")
        or p.get("stopped_previous")
        # 换运行时期间排的队：等得久是平台的原因，已经告诉过会自动处理。
        or p.get("queued_migrating")
    ):
        return False
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_busy_choice.py tests/integration/test_busy_merge.py tests/integration/test_assistant_gateway_inbound.py tests/integration/test_gateway_wecom.py -v`
Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add coreman/runtime/gateway_common/inbound.py coreman/runtime/worker/chat/queued_batch.py coreman/core/i18n/messages.py tests/integration/test_busy_choice.py
git commit -m "fix(gateway): 换运行时期间的新消息给一句提示，不弹忙碌卡也不再二次确认"
```

---

### Task 6: 换运行时关掉后台会话时告诉提问人

换运行时会关掉这个员工所有在后台阶段的会话，状态直接写成「已停止」。因为状态已经是终态，后面的收尾流程不会再发通知，卡片也不刷新：提问人不知道后台任务被停了。

**Files:**
- Modify: `coreman/core/chat/background_runs.py`（`close_for_bot`、`queue_notice`）
- Modify: `coreman/core/bots/switch_relay.py:259-262`
- Modify: `coreman/core/i18n/messages.py`
- Test: `tests/integration/test_background_runs.py`

**Interfaces:**
- Produces: `close_for_bot(session, bot_id, reason="reset", *, notice: str | None = None) -> int`；`queue_notice(session, run, locale="zh", *, key: str | None = None)`；文案键 `background_switched_notice`（参数 `elapsed`、`lines`）。

- [ ] **Step 1: 写失败的测试**

加到 `tests/integration/test_background_runs.py` 的 `test_clear_bot_paths_close_every_running_phase` 之后（`feishu_bot`、`make_run` 是文件里已有的助手）：

```python
async def test_runtime_switch_tells_the_asker_that_background_work_was_stopped(
    db_session: AsyncSession,
) -> None:
    from coreman.core.db.models import OutboxItem
    from coreman.core.i18n.messages import msg

    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    closed = await background_runs.close_for_bot(
        db_session, bot.id, notice="background_switched_notice"
    )
    await db_session.commit()
    await db_session.refresh(run)
    assert closed == 1 and run.state == "stopped"
    assert all(t.get("status") != "running" for t in run.tasks or [])
    notice = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"bg-end:{run.id}")
    )
    assert notice is not None
    prefix = msg("background_switched_notice", elapsed="", lines="").split("（")[0]
    assert notice.payload["markdown"].startswith(prefix)
    card = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"bg-card:{run.id}:switch")
    )
    assert card is not None
```

文件顶部缺 `select` 的 import 时补上。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_background_runs.py::test_runtime_switch_tells_the_asker_that_background_work_was_stopped -v`
Expected: FAIL，`close_for_bot` 不接受 `notice`。

- [ ] **Step 3: 文案**

`coreman/core/i18n/messages.py`，`zh` 表里 `background_stopped_notice` 之后：

```python
        "background_switched_notice": (
            "⏹ 这位 AI 员工的运行时或模型刚换过，后台任务随之停止（已跑 {elapsed}）。没做完的：\n"
            "{lines}\n\n"
            "需要的话说「接着做」，我会看会话记录从停下的地方继续。"
        ),
```

`ja` 表里 `background_stopped_notice` 之后：

```python
        "background_switched_notice": (
            "⏹ この AI 社員のランタイムまたはモデルが切り替わったため、バックグラウンドタスクを"
            "停止しました（実行時間 {elapsed}）。未完了：\n{lines}\n\n"
            "必要であれば「続けて」と伝えてください。会話の記録を見て、止まったところから続けます。"
        ),
```

- [ ] **Step 4: 通知可指定文案**

`coreman/core/chat/background_runs.py` 的 `queue_notice`，签名与选文案的部分改成：

```python
async def queue_notice(
    session: AsyncSession, run: BackgroundRun, locale: str = "zh", *, key: str | None = None
) -> None:
    """被停、到期、丢失：主动告诉提问人（回复在原消息下并 @ 他），列出没做完的和已跑时长。

    `key` 给定时用这条文案（参数同样是 elapsed、lines），如换运行时关掉的那一种。
    """
    from coreman.core.bus import outbox
    from coreman.core.i18n.messages import msg

    now = datetime.now(UTC)
    elapsed = minutes((now - run.created_at).total_seconds())
    lines = "\n".join(unfinished_lines(run, now)) or "（无）"
    if key is not None:
        text = msg(key, locale, elapsed=elapsed, lines=lines)
    elif run.state == "lost":
        text = msg("background_lost_notice", locale, elapsed=elapsed)
    elif run.state == "expired":
        text = msg("background_expired_notice", locale, elapsed=elapsed, lines=lines)
    else:
        text = msg("background_stopped_notice", locale, elapsed=elapsed, lines=lines)
```

函数余下的 `outbox.add(...)` 不动。

- [ ] **Step 5: 关闭时带通知**

同一文件的 `close_for_bot` 改成：

```python
async def close_for_bot(
    session: AsyncSession, bot_id: uuid.UUID, reason: str = "reset", *, notice: str | None = None
) -> int:
    """换运行时、换模型、换工作目录前：这个员工所有在后台阶段的会话都关掉。

    `notice` 给定时（换运行时）：没做完的项记为停止、刷新卡片，并用这条文案告诉提问人。
    这里直接写了终态，之后驱动回来的收尾不会再通知，不在这里说就没人知道被停了。
    """
    rows = list(
        await session.scalars(
            select(BackgroundRun)
            .where(BackgroundRun.bot_id == bot_id, BackgroundRun.state == "running")
            .with_for_update()
        )
    )
    for run in rows:
        await request_close(session, run, reason)
        if notice is None:
            await end(session, run, CLOSE_STATES[reason])
            continue
        # 与 conclude 同一套收尾：还在跑的项记为停止，定终态，刷新卡片，告诉提问人。
        run.tasks = [
            {**t, "status": "stopped"} if t.get("status") == "running" else t
            for t in run.tasks or []
        ]
        if await end(session, run, CLOSE_STATES[reason]):
            await queue_card(session, run, "switch")
            await queue_notice(session, run, key=notice)
    return len(rows)
```

- [ ] **Step 6: 换运行时调用时带上通知**

`coreman/core/bots/switch_relay.py`，把

```python
    await background_runs.close_for_bot(session, bot.id)
```

改成

```python
    await background_runs.close_for_bot(session, bot.id, notice="background_switched_notice")
```

- [ ] **Step 7: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_background_runs.py tests/integration/test_background_push.py tests/integration/test_switch_jobs.py tests/api/test_workspace_migration.py -v`
Expected: 全部 PASS。

- [ ] **Step 8: Commit**

```bash
git add coreman/core/chat/background_runs.py coreman/core/bots/switch_relay.py coreman/core/i18n/messages.py tests/integration/test_background_runs.py
git commit -m "fix(switch): 换运行时关掉后台会话时刷新卡片并告诉提问人"
```

---

### Task 7: 全量检查、变更说明与 PR

**Files:**
- Modify: `CHANGELOG.md`（最上面的未发布小节）
- Modify: `docs/features/runtime-seats.md`（「额度」一节后补一句）

- [ ] **Step 1: 全量检查**

Run: `uv run ruff check . && uv run mypy coreman && uv run pytest`
Expected: 全部通过。失败的用例如果是断言了被本计划改掉的旧行为（撞限轮次算成功、回执旧文案），按新行为改期望值；其它失败先查原因，不改断言。

Run（在 `runtime_daemon/drivers` 下）: `go vet ./... && go test ./...`
Expected: 全部通过。

- [ ] **Step 2: 变更说明**

`CHANGELOG.md` 的 `## [Unreleased]` 下 `### Fixed` 小节里加：

```markdown
- 运行时额度用完时的告示不再被当成回答：对话、定时任务、任务会话都按「额度中断」记为失败。
- 换席位后，管理台显示的账号立刻换成新席位的（需要升级运行时节点）。
- 换运行时的文字续接改为按会话沿革取，换走再换回不再丢最初那一段对话（迁移 0139）。
- 切换回执如实说明有几个对话没能原样带过去；Codex、OpenCode 带了续接文本时不再提示「会话记录已不存在」（需要升级运行时节点）。
- 换运行时期间发来的消息会收到一句「换好后自动处理」，不再弹出「上一条还在处理」的卡片。
- 换运行时关掉后台任务时会刷新卡片并告诉提问人。
```

迁移编号顺延过的，这里写实际编号。

- [ ] **Step 3: 席位文档补一句**

`docs/features/runtime-seats.md` 的「额度」一节末尾加一段：

```markdown
切换后节点列表里的账号立刻换成新席位的，不用等下一次心跳。
```

- [ ] **Step 4: 开源隔离自查**

Run:

```bash
git diff main...HEAD -U0 | grep '^+' | grep -n -E '/Users/|/home/[a-z]+/' || echo clean
```

Expected: `clean`（没有本机绝对路径）。再通读一遍 `git diff main...HEAD`，确认新增内容里没有公司名、内部域名、内部机器名、真实账号；测试里的账号只用 `example.com`。

- [ ] **Step 5: Commit 并开 PR**

```bash
git add CHANGELOG.md docs/features/runtime-seats.md
git commit -m "docs: 记录换运行时体验修复"
git push -u origin fix/runtime-switch-phase0
```

PR 标题：`fix: 换运行时体验修复（全自动切换第 0 期）`。描述里写明：六项各自修了什么；迁移编号；哪两项要升级运行时节点、不升级时的表现（平台照常工作，只是节点侧的两处修复不生效）；没在真机上验证的部分。末尾带 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。
