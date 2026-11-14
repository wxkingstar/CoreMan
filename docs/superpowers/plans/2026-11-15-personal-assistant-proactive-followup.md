# 个人助理：主动跟进（自己说过的话、重点人、每日与每周例行） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让助理的跟进覆盖所有者自己说出口的事（他答应别人的、他向别人要的），让他指定的重点人的消息总是立即交回，并在每天早上、每周最后一个工作日各做一次例行整理。

**Architecture:** 全部复用现有的「收件箱条目 → 交回一轮 → 待汇报进摘要」通路，不加新的发送通道，平台侧不新增任何飞书调用。所有者自己发的消息作为一种新的收件箱条目（`owner_said`）攒 30 分钟一批交回，助理只提取承诺与索要、不起草回复。重点人是设置里新增的一份名单，由纯函数分级使用。每日与每周例行是两种新的提醒条目，由每 5 分钟一次的顺带检查按所有者当地时间各生成一次，日程与任务由助理在那一轮里用已有的读工具自己查。

**Tech Stack:** Python 3.12、SQLAlchemy async、Alembic、pytest（`uv run pytest`）。

**Spec:** `docs/superpowers/specs/2026-11-11-personal-assistant.md`（第五节「分级」「投递与会话」；Task 5 把本计划的行为补进去）

## Global Constraints

- 没事不花模型额度：分级与例行提醒的生成都不经模型；新增的条目都是攒批级别，每日轮次预算用完时留到次日。
- 所有者自己发的消息只用于提取待跟进：不起草回复、不对外做任何事、不出声。
- 平台经审批代所有者发出的消息（`assistant_sent`）不算他亲自发言，不入箱。
- 所有者与助理自己的私聊（`AssistantSettings.chat_id`）里的消息不入箱：那里的话已经是给助理的。
- 个人内容只进加密列：重点人名单是和谁关系近的信息，加密保存并在 `sealed.COLUMNS` 登记。
- 设置只在所有者当面的轮次里能读能改（沿用现有的 `owner_turn_required`）。
- 迁移编号接在现有最新的之后（写计划时最新是 `0110`，本计划用 `0111`；开工时若主干已有 `0111`，顺延并在报告里说明）。迁移文件开头的说明用英文，与现有迁移一致；`upgrade` 开头 `SET LOCAL lock_timeout = '5s'`。
- 公开仓：示例用通用占位（张三、李四、`example.com`），不出现公司名、内部域名、内部系统名、本机路径。
- 提交信息用中文一句话说清行为变化，末尾加 `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`。
- 每个任务结束前跑 `uv run ruff check .` 与 `uv run mypy coreman`，都要通过。

## 设计选择

1. **所有者的发言攒 30 分钟一批。** 他聊得正起劲时不必每 5 分钟开一轮；承诺与索要也不差这半小时。同一批至多 30 条（一轮的上限），多的下一轮带。按一天活跃 8 小时算，这一项至多多出十几轮，默认每日预算是 2000 轮。
2. **只收文本、够长的。** `msg_type` 不是文本类（`text`、`post`）的不收；压成单行后不足 4 个字的不收（「好」「收到」「ok」里没有承诺也没有索要）。不做关键词匹配：措辞因人而异，交给模型判断。
3. **`owner_said` 条目不触发「紧急」。** 它固定是攒批级别；同一轮里若另有立即级别的条目，那一轮记下的汇报照旧按紧急发，这是现有行为，不改。
4. **重点人用 open_id 名单，上限 10 人。** 所有者说「重点关注张三」时由助理用通讯录工具查到 open_id 再写设置。重点人在任何会话里发的消息都立即交回（群里没 @ 他也算）；忽略名单、撤回、系统消息、他刚在该会话发过言（压着）仍然优先。重点人发来的事引起的那一轮，汇报按紧急发（现有的「立即级别引起的一轮按紧急发」）。
5. **例行整理做成提醒条目，日程与任务由助理自己查。** 平台不新增日历、任务的调用，也不改摘要卡片：助理在那一轮里用已有的读工具查今明两天的日程和到期任务，用 `assistant_report` 记成待汇报，随下一次摘要发出；所有者没有授权这些读权限时工具会如实报错，助理就只汇报待跟进。没有内容时不记，摘要照旧不发。
6. **例行的时点。** 每日一次：所有者当地时间到了当天第一个摘要时点之前 30 分钟起生成（第一轮摘要因此带得上）。每周一次：在他设置的工作日里最后一天、到了当天最后一个摘要时点之前 60 分钟起生成。两者的去重键里带日期或 ISO 周，重复检查不会重复生成；不在班时条目留着，上班后交回。
7. **每周例行只动待跟进，不自动改知识库。** 把未结的待跟进过一遍（办完的关、过期的推进或推后），按四类各记一条汇报。知识库的体检留给每日整理，不在这里加。

## File Structure

| 文件 | 责任 |
|---|---|
| Create `migrations/versions/0111_assistant_owner_said.py` | 收件箱 `kind` 约束加 `owner_said`；`assistant_settings` 加 `focus_people_enc` |
| Modify `coreman/core/db/models/assistants.py` | `INBOX_KINDS`、`AssistantSettings.focus_people_enc` |
| Modify `coreman/core/assistants/sealed.py` | 登记 `focus_people_enc` |
| Modify `coreman/core/assistants/watch.py`、`watch_inbox.py` | 所有者发言入箱 |
| Modify `coreman/core/assistants/handover.py`、`approvals/proxied.py` | 新条目与新提醒的标题和正文 |
| Modify `coreman/core/assistants/handover_prompt.py` | 三种新条目怎么处理 |
| Modify `coreman/core/assistants/watch_grade.py` | 重点人 |
| Modify `coreman/core/assistants/settings.py`、`settings_tools.py`、`settings_prompt.py` | 重点人名单的读写 |
| Modify `coreman/core/assistants/delivery_checks.py` | 每日与每周例行提醒 |
| Tests | 见各任务 |
| Modify spec | 第五节 |

---

### Task 1: 迁移与模型

**Files:**
- Create: `migrations/versions/0111_assistant_owner_said.py`
- Modify: `coreman/core/db/models/assistants.py`、`coreman/core/assistants/sealed.py`
- Test: `tests/integration/test_migration_assistant_owner_said.py`（照 `tests/integration/test_migration_assistant_settings.py` 的写法）、`tests/unit/test_assistant_sealed_ledger.py`（按需登记）

**Interfaces:**
- Produces: `INBOX_KINDS = ("message", "approval_todo", "reminder", "owner_said")`；`AssistantSettings.focus_people_enc`（JSONB，封存，默认空列表，可空与 `focus_chats_enc` 一致）

- [ ] **Step 1: 写失败的迁移测试**

照 `test_migration_assistant_settings.py` 里升级、降级用例的结构写三个断言：升级后能插入 `kind = 'owner_said'` 的收件箱行；升级后 `assistant_settings` 有 `focus_people_enc` 列；降级后该列不在、`kind = 'owner_said'` 的行被约束拒绝（降级前测试自己先删掉这种行）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_migration_assistant_owner_said.py -q`
Expected: FAIL（没有 0111）

- [ ] **Step 3: 写迁移**

```python
"""Assistant inbox kind `owner_said` and the focus-people list.

`owner_said` rows hold the owner's own outgoing messages so the assistant can extract
promises and requests from them. `focus_people_enc` is the sealed list of open_ids whose
messages are always handed over immediately.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

OLD = "kind IN ('message', 'approval_todo', 'reminder')"
NEW = "kind IN ('message', 'approval_todo', 'reminder', 'owner_said')"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("kind", "assistant_inbox", type_="check")
    op.create_check_constraint("kind", "assistant_inbox", NEW)
    op.add_column(
        "assistant_settings",
        sa.Column("focus_people_enc", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("assistant_settings", "focus_people_enc")
    op.execute("DELETE FROM assistant_inbox WHERE kind = 'owner_said'")
    op.drop_constraint("kind", "assistant_inbox", type_="check")
    op.create_check_constraint("kind", "assistant_inbox", OLD)
```

约束名要与库里实际的一致：先看 `0097_assistant_watch.py` 是怎么建这条约束的（`op.create_check_constraint` 传的名字，以及命名约定是否会加前缀），`drop_constraint` 用同一种写法。`focus_people_enc` 的列类型、可空性、默认值照 `focus_chats_enc` 在它自己的迁移里的定义抄，不要自己定。

- [ ] **Step 4: 改模型与封存登记**

`models/assistants.py`：`INBOX_KINDS` 末尾加 `"owner_said"`；`AssistantSettings` 里紧跟 `ignored_chats_enc` 之后加 `focus_people_enc`，定义照 `focus_chats_enc` 那一行抄，注释写「重点人：这些 open_id 发的消息总是立即交回」。

`sealed.py` 里 `AssistantSettings` 的登记加一项：`"focus_people_enc": Whole([])`。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/integration/test_migration_assistant_owner_said.py tests/integration/test_assistant_watch_schema.py tests/integration/test_assistant_settings_schema.py tests/unit/test_assistant_sealed_ledger.py -q`
Expected: 全部通过。结构类测试若逐项列了 `INBOX_KINDS` 或设置列，按新增的补上；台账测试若因新列失败，如实登记并在报告里说明理由。

- [ ] **Step 6: 提交**

```bash
git add migrations/versions/0111_assistant_owner_said.py coreman/core/db/models/assistants.py coreman/core/assistants/sealed.py tests
git commit -m "个人助理迁移 0111：收件箱加 owner_said 条目类型，设置加重点人名单列"
```

---

### Task 2: 所有者自己发的消息交回，提取承诺与索要

**Files:**
- Modify: `coreman/core/assistants/watch.py`（`_Round.take` 的所有者分支）、`coreman/core/assistants/watch_inbox.py`（`add`）
- Modify: `coreman/core/assistants/handover.py`（`_title` 与 `render` 的正文分支）、`coreman/core/assistants/approvals/proxied.py`（本轮交回事项的标题）
- Modify: `coreman/core/assistants/handover_prompt.py`
- Test: `tests/integration/test_assistant_watch_poll.py`、`tests/integration/test_assistant_watch_inbox.py`、处理 `handover.render` 的现有测试文件、`tests/unit/test_assistant_handover_prompt.py`

**Interfaces:**
- Consumes: `INBOX_KINDS` 含 `owner_said`（Task 1）
- Produces:
  - `watch_inbox.add(session, bot_id, item, level, *, owner_open_id, hold_until, kind: str = "message") -> bool`
  - `watch.OWNER_SAID_WINDOW = timedelta(minutes=30)`、`watch.OWNER_SAID_MIN_CHARS = 4`、`watch.OWNER_SAID_TYPES = frozenset({"text", "post"})`
  - `handover.OWNER_SAID_TITLE = "你自己发的消息"`

- [ ] **Step 1: 写失败的测试**

`tests/integration/test_assistant_watch_poll.py`，用这个文件现有的辅助函数（`assistant`、`install`、`msg`、`primed`、`tick`、`inbox_rows`）写四个用例：

1. `test_the_owners_own_message_is_boxed_as_owner_said`：所有者在一个群里发「周四前我把报价发你」；一轮之后收件箱有一行 `kind == "owner_said"`、`level == "batch"`、`hold_until` 约等于消息时间之后 30 分钟（`OWNER_SAID_WINDOW`）、`summary_enc["text"]` 是那句话；这个会话照旧被压着（现有行为：别人的消息 `hold`）。
2. `test_short_or_non_text_owner_messages_are_not_boxed`：所有者发「好的」、一张图片（`msg_type="image"`）；收件箱没有 `owner_said` 行。
3. `test_messages_the_platform_sent_for_the_owner_are_not_boxed`：先在 `AssistantSent` 里登记一条消息编号，再让这条「所有者发的」消息被读到；没有 `owner_said` 行。
4. `test_the_owners_chat_with_the_assistant_is_not_boxed`：消息所在会话就是 `prefs.chat_id`；没有 `owner_said` 行。

再加一个 `test_owner_said_items_share_one_batch`：10 分钟内所有者在两个会话各发一句，两行的 `hold_until` 相同（并进同一批）。

处理 `handover.render` 的测试文件里加：一行 `kind="owner_said"` 的条目渲染出的标题含 `handover.OWNER_SAID_TITLE`，正文里那句话在「> 」引用行里，且不出现「私聊消息」「群聊消息」。

`tests/unit/test_assistant_handover_prompt.py` 加：

```python
def test_rules_say_what_to_do_with_the_owners_own_messages() -> None:
    text = handover_prompt.RULES
    assert handover.OWNER_SAID_TITLE in text
    for phrase in ("promised", "waiting", "不起草回复", "先用 followup_list 看有没有同一件事"):
        assert phrase in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration/test_assistant_watch_poll.py tests/unit/test_assistant_handover_prompt.py -q -k "owner"`
Expected: 新用例 FAIL

- [ ] **Step 3: 实现入箱**

`watch_inbox.add` 加仅关键字参数 `kind: str = "message"`，写入的 `"kind"` 用它；去重索引里本来就有 `kind`，同一条消息可以同时有 `message` 与 `owner_said`（实际不会同时出现）。

`watch.py` 常量区加：

```python
# 所有者自己发的消息攒这么久一批交回：只用来提取承诺与索要，不差这半小时。
OWNER_SAID_WINDOW = timedelta(minutes=30)
# 压成单行后不足这么多字的不收（「好」「收到」里没有承诺也没有索要）。
OWNER_SAID_MIN_CHARS = 4
OWNER_SAID_TYPES = frozenset({"text", "post"})
```

`_Round.take` 的所有者分支，在 `hold_chat` 之后、`return` 之前加：

```python
            await self.owner_said(item)
```

并给 `_Round` 加方法：

```python
    async def owner_said(self, item: Item) -> None:
        """所有者亲自发的一句话入箱（攒批）：交回后助理只从中提取承诺与索要。

        与助理自己的私聊、非文本、太短的不收；太老的同样不收（与别人的消息同一条截止线）。
        """
        if item.chat_id == self.prefs.chat_id or item.msg_type not in OWNER_SAID_TYPES:
            return
        if len(" ".join(item.text.split())) < OWNER_SAID_MIN_CHARS:
            return
        if item.at_ms < _week_ago(self.now):
            return
        until = await inbox.batch_until(self.session, self.bot_id, kind="owner_said")
        if until is None or until <= self.now:
            until = self.now + OWNER_SAID_WINDOW
        await inbox.add(
            self.session,
            self.bot_id,
            item,
            Level.BATCH,
            owner_open_id=self.prefs.open_id,
            hold_until=until,
            kind="owner_said",
        )
```

`inbox.batch_until`：先看 `watch_inbox.py` / `watch.py` 里别人的攒批条目是怎么取「正在攒的那一批什么时候到点」的（传给 `release_at(..., batch_until=...)` 的那个值从哪来），按同样的查法给 `owner_said` 取一个：这位助理 `kind == "owner_said"`、`status == "pending"`、`delivered_task_id IS NULL` 的行里最大的 `hold_until`。已有等价函数能加 `kind` 过滤就加参数复用，没有就在 `watch_inbox.py` 里加这个函数。两种做法都要保证：别人的消息的攒批窗口不受 `owner_said` 行影响（原查询要排除 `kind == "owner_said"`，否则别人的攒批会被拉长到 30 分钟）——给这一点补一个用例。

`hold_chat`（压着这个会话）会不会把刚入箱的 `owner_said` 行也推后：读 `watch_inbox.hold_chat` 的条件；会的话让它跳过 `kind == "owner_said"` 的行。

- [ ] **Step 4: 实现渲染与提示词**

`handover.py`：加 `OWNER_SAID_TITLE = "你自己发的消息"`；`_title` 里 `row.kind == "owner_said"` 返回它；正文沿用 `_message(row, data)`（定位、会话、引用的原话都需要）。`approvals/proxied.py` 里给「本轮交回的事项」起标题的那段，`owner_said` 用同一个标题。

`handover_prompt.py` 的 `RULES`，在「提醒类条目」那一条之前加一条：

```python
    "- 标题是「你自己发的消息」的条目：那是所有者亲自发出去的话，不是别人发给他的。不起草回复，"
    "也不用汇报；只看里面有没有两样东西——他答应了别人一件事（「我看下」「周四给你」），用 "
    "followup_add 记一条 promised；他向别人要东西或请人办事（「发我一下」「帮我建个…」），"
    "记一条 waiting，写清要的是什么、找的谁。记之前先用 followup_list 看有没有同一件事，有就用 "
    "followup_update 补充；他这句话说明某件待跟进已经办完的，用 followup_close 关掉。两样都"
    "没有就略过。origin_message_id 填这条消息。\n"
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit -q -k "handover or watch"`，再跑集成：`tests/integration -k "assistant_watch or assistant_handover or assistant_delivery"`
Expected: 全部通过。现有用例里若有「所有者发言之后收件箱为空」之类的断言，改成「没有 `kind == "message"` 的新行」或把那句话换成不足 4 个字的，保持原用例的意图。

- [ ] **Step 6: 提交**

```bash
git add coreman/core/assistants tests
git commit -m "个人助理盯消息：所有者自己发的话攒 30 分钟一批交回，助理从中提取他答应的事和向别人要的东西记成待跟进"
```

---

### Task 3: 重点人

**Files:**
- Modify: `coreman/core/assistants/watch_grade.py`
- Modify: `coreman/core/assistants/settings.py`（`update`、`view`、校验）、`settings_tools.py`（`Update`、工具说明）、`settings_prompt.py`
- Test: `tests/unit/test_assistant_watch_grade.py`、`tests/unit/test_assistant_settings.py`、`tests/integration/test_assistant_settings_tools.py`、`tests/unit/test_assistant_settings_prompt.py`

**Interfaces:**
- Consumes: `AssistantSettings.focus_people_enc`（Task 1）
- Produces: `settings.MAX_FOCUS_PEOPLE = 10`；`settings.update(..., focus_people: list[str] | None = None)`；`view()` 返回里多一项 `"focus_people"`；工具入参 `focus_people`

- [ ] **Step 1: 写失败的测试**

`tests/unit/test_assistant_watch_grade.py`：`prefs()` 里加 `focus_people_enc=["ou_boss"]`，`TABLE` 加：

```python
    ("重点人在群里没 @ 他", item(sender_id="ou_boss"), None, Level.IMMEDIATE),
    ("重点人私聊", item(**P2P, sender_id="ou_boss"), None, Level.IMMEDIATE),
    ("重点人在忽略的会话里", item(chat_id="oc_noise", sender_id="ou_boss"), None, Level.IGNORE),
    ("他刚在这个会话发过言时重点人的消息", item(sender_id="ou_boss"), timedelta(minutes=1), Level.HOLD),
    ("机器人的编号碰巧在重点人名单里", item(**{**BOT, "sender_id": "ou_boss"}), None, Level.IGNORE),
```

`tests/unit/test_assistant_settings.py`（照文件里校验重点群的用例写）：`focus_people` 接受 `ou_` 开头的编号、去重去空白；不是 `ou_` 开头的抛 `invalid_people`；超过 10 个抛 `too_many_people`。

`tests/integration/test_assistant_settings_tools.py`：`assistant_settings_update` 传 `focus_people=["ou_boss"]` 后，`assistant_settings_get` 的返回里 `focus_people == ["ou_boss"]`；无人在场的轮次调用返回 `owner_turn_required`（沿用现有行为，照文件里对应用例写）。

`tests/unit/test_assistant_settings_prompt.py`：`"focus_people" in settings_prompt.SETTINGS_RULES` 且 `"通讯录" in settings_prompt.SETTINGS_RULES`。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit -q -k "watch_grade or assistant_settings"`
Expected: 新用例 FAIL

- [ ] **Step 3: 实现**

`watch_grade.py` 的 `grade`（在 Task「重点群」之后的版本上改）：算出 `human` 之后加

```python
    vip = human and item.sender_id in (settings.focus_people_enc or [])
```

并把最后的立即判断

```python
    if mentioned or focus:
        return Level.IMMEDIATE
```

改成

```python
    if mentioned or focus or vip:
        return Level.IMMEDIATE
```

私聊里真人本来就立即，不用另改。模块开头的分级说明同步加一句「所有者标记的重点人发的消息」。

`settings.py`：加 `MAX_FOCUS_PEOPLE = 10` 与 `_PERSON = re.compile(r"^ou_[A-Za-z0-9_-]{1,80}$")`；照 `_chats(values, limit)` 写 `_people(values, limit)`（去空白、去重保序；不匹配抛 `SettingsError("invalid_people")`，超限抛 `SettingsError("too_many_people", limit=limit)`，异常的构造方式与 `_chats` 里的一致）。`update` 的签名加 `focus_people: list[str] | None = None`，把它加进「至少给一项」的那个元组；写入前与另两份名单一样先核 `sealed.key_ready`；`row.focus_people_enc = _people(focus_people, MAX_FOCUS_PEOPLE)`。`view` 的返回加 `"focus_people": list(row.focus_people_enc or [])`。

`settings_tools.py`：`Update` 加字段

```python
    focus_people: Annotated[list[Short], Field(max_length=50)] | None = Field(
        default=None,
        description="Whole list of open_ids (ou_...) whose messages are always handed over at once",
    )
```

（`Short` 与 `Field` 的用法照 `focus_chats` 那一项；工具说明文字里提到可改项的地方加上重点人。）

`settings_prompt.py`：在说明 `focus_chats` 的那一条之后加

```python
    f"- focus_people 是重点人（最多 {settings.MAX_FOCUS_PEOPLE} 个）：他们在任何会话里发的消息都"
    "立刻交回，群里没 @ 所有者也算。名单是 open_id（ou_ 开头），整份替换。所有者只说了名字时，"
    "先用通讯录工具查到 open_id；重名或查不到就问他，不要猜。\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit -q -k "watch_grade or assistant_settings or settings"`，集成：`tests/integration -k "assistant_settings"`
Expected: 全部通过。「我的助理」页的设置接口若逐项列了设置的键（`tests/api` 里搜 `focus_chats`），返回里多了 `focus_people` 时按实际更新断言；页面是否提供编辑入口不在本任务内。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants tests
git commit -m "个人助理设置：加重点人名单，他们在任何会话里发的消息都立即交回"
```

---

### Task 4: 每日与每周例行

**Files:**
- Modify: `coreman/core/assistants/delivery_checks.py`（新增 `routines`，在 `sweep` 里调用）
- Modify: `coreman/core/assistants/handover.py`（`REMINDERS` 与 `_reminder`）
- Modify: `coreman/core/assistants/handover_prompt.py`
- Test: 覆盖 `delivery_checks.sweep` / `followups_due` 的现有集成测试文件（搜 `followups_due` 找到它）、`tests/unit/test_assistant_handover_prompt.py`

**Interfaces:**
- Consumes: `add_reminder(session, bot_id, key, *, level, summary, now, chat_id=None) -> bool`、`assistant_settings.digest_slots(prefs) -> list[str]`、`assistant_settings.local_today(prefs, now)`、`prefs.workdays`
- Produces: `delivery_checks.routines(session, bot_id, prefs, now) -> int`；提醒子类型 `morning_brief`、`weekly_review`；`sweep` 的计数里多一项 `"assistant_routines"`

- [ ] **Step 1: 写失败的测试**

在覆盖 `followups_due` 的集成测试文件里，用它现有的造数辅助函数，写（时间都按所有者时区构造；设 `digest_times = ["09:00", "18:00"]`、免打扰关掉、`workdays = [1, 2, 3, 4, 5]`）：

1. `test_morning_brief_is_added_once_from_half_an_hour_before_the_first_digest`：当地 08:29 调 `routines` 返回 0、没有提醒行；08:30 返回 1，收件箱多一行 `kind == "reminder"`、`level == "batch"`、`summary_enc == {"reminder": "morning_brief"}`；08:35 再调返回 0。次日 08:30 又是 1。
2. `test_weekly_review_is_added_on_the_last_workday_before_the_last_digest`：周四 17:00 返回里不含每周（只可能有当天已生成过的每日）；周五 16:59 没有 `weekly_review`；周五 17:00 有一行 `summary_enc == {"reminder": "weekly_review"}`；同一周再调不重复；所有者把工作日设成 `[1, 2, 3]` 时周三是最后一个工作日。
3. `test_routines_wait_until_cold_start_is_done`：`cold_start_done_at` 为空时什么都不生成。
4. `test_sweep_counts_routines`：`sweep(...)` 的返回里有键 `"assistant_routines"`。

`tests/unit/test_assistant_handover_prompt.py` 加：

```python
def test_rules_say_what_the_two_routines_are() -> None:
    text = handover_prompt.RULES
    assert handover.REMINDERS["morning_brief"] in text
    assert handover.REMINDERS["weekly_review"] in text
    for phrase in ("今明两天的日程", "到期的任务", "没有就什么都不记", "四类各记一条"):
        assert phrase in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/integration -q -k "routine or morning_brief or weekly_review"` 与 `uv run pytest tests/unit/test_assistant_handover_prompt.py -q`
Expected: 新用例 FAIL

- [ ] **Step 3: 实现**

`delivery_checks.py`：

```python
# 每日例行在当天第一个摘要时点之前这么久起生成，第一轮摘要带得上；每周例行在最后一个时点之前。
MORNING_LEAD = timedelta(minutes=30)
WEEKLY_LEAD = timedelta(minutes=60)


def _at(day: date, slot: str, prefs: AssistantSettings) -> datetime:
    """所有者当地 `day` 的 `slot`（HH:MM）对应的 UTC 时刻。"""
    hour, minute = (int(part) for part in slot.split(":"))
    zone = assistant_settings.zone(prefs)
    return datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(UTC)


async def routines(
    session: AsyncSession, bot_id: uuid.UUID, prefs: AssistantSettings, now: datetime
) -> int:
    """每日与每周例行的提醒条目，各自到点生成一次。不调飞书：日程与任务由助理在那一轮里自己查。"""
    if prefs.cold_start_done_at is None:
        return 0
    slots = assistant_settings.digest_slots(prefs)
    if not slots:
        return 0
    today = assistant_settings.local_today(prefs, now)
    added = 0
    if now >= _at(today, slots[0], prefs) - MORNING_LEAD:
        added += await add_reminder(
            session,
            bot_id,
            f"morning_brief:{today.isoformat()}",
            level="batch",
            summary={"reminder": "morning_brief"},
            now=now,
        )
    workdays = sorted(prefs.workdays or [])
    if (
        workdays
        and today.isoweekday() == workdays[-1]
        and now >= _at(today, slots[-1], prefs) - WEEKLY_LEAD
    ):
        year, week, _ = today.isocalendar()
        added += await add_reminder(
            session,
            bot_id,
            f"weekly_review:{year}-W{week:02d}",
            level="batch",
            summary={"reminder": "weekly_review"},
            now=now,
        )
    return added
```

取所有者时区：`settings.py` 里已有按 `row.timezone` 返回 `ZoneInfo` 的函数（约第 66–71 行，读不懂的时区回落到默认时区）；用它的实际名字替换上面的 `assistant_settings.zone`，不要另写一份。`time`、`date`、`UTC` 的导入按文件现状补齐。

`sweep` 的 `counts` 里加一项：

```python
        "assistant_routines": await routines(session, bot.id, prefs, now),
```

`handover.py`：`REMINDERS` 加

```python
    "morning_brief": "每日例行：今天要留意的事",
    "weekly_review": "每周例行：把待跟进过一遍",
```

`_reminder(data)` 里这两种没有附带字段：返回一行平台写的说明即可（「按『交回事项』里对这类条目的说明做。」），与其他提醒的写法保持同一风格；不要让它们落进需要 `data` 里别的键的分支。

`handover_prompt.py` 的 `RULES`，在「提醒类条目」那一条之后加两条：

```python
    "- 「每日例行：今天要留意的事」：用日历和任务的读工具查所有者今明两天的日程、今天与已经"
    "到期的任务，再用 followup_list 看今天该回头看的待跟进；把他今天要留意的用 assistant_report "
    "记成一两条（时间冲突、要他准备的会、到期的任务、到期该拍板的事），普通的例会不用列。读工具"
    "报没有权限时跳过那一项，不要去发起授权。没有就什么都不记。\n"
    "- 「每周例行：把待跟进过一遍」：用 followup_list 把未结的待跟进看一遍——已经办完的关掉，"
    "过了回看日的推进或用 followup_snooze 推后，等别人而对方一直没动静的考虑起草一条催办。然后"
    "按 decide、promised、waiting、remind 四类各记一条 assistant_report（这一类还有几件、最要紧"
    "的是哪一两件），空的类不记。\n"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit -q -k "handover"`，集成：`tests/integration -k "assistant_delivery or routine or handover"`
Expected: 全部通过。现有用例若断言了 `sweep` 返回的全部键或「这一轮没有任何提醒行」，按新增的计数与条目更新（测试时刻落在生成窗口里的，优先把测试时刻挪到窗口外，保持原用例的意图）。

- [ ] **Step 5: 提交**

```bash
git add coreman/core/assistants tests
git commit -m "个人助理例行整理：每天早上查今明日程与到期任务，每周最后一个工作日把待跟进过一遍，结果随摘要发出"
```

---

### Task 5: 文档与全量检查

**Files:**
- Modify: `docs/superpowers/specs/2026-11-11-personal-assistant.md`

- [ ] **Step 1: 补 spec（第五节）**

分级表：
- 「立即交回」一行的条件末尾加「；所有者标记的重点人发的消息」。
- 「攒批」一行的条件末尾加「；所有者自己发的文本消息（见下）；每日与每周例行」。
- 「忽略」一行里的「所有者自己发的」改为「所有者自己发的非文本或不足 4 个字的消息、他与助理自己的私聊里的消息、平台代他发出的消息」。

在「投递与会话」之前加一小节：

```markdown
### 所有者自己说过的话与例行整理

- **所有者自己发的消息**：文本类、压成单行不少于 4 个字、不在他与助理的私聊里、不是平台代发的，作为 `owner_said` 条目入箱，攒 30 分钟一批交回。助理只从中提取两样：他答应别人的事（记 `promised`）、他向别人要的东西（记 `waiting`）；不起草回复、不汇报。它照旧压住这个会话 10 分钟。
- **重点人**：设置里的一份 open_id 名单（至多 10 人，加密保存），他们在任何会话里发的消息都立即交回；忽略的会话、撤回、压着仍然优先。由所有者在私聊里说、助理用通讯录工具查到 open_id 后写入。
- **每日例行**：所有者当地时间到了当天第一个摘要时点之前 30 分钟，平台生成一条提醒；助理在那一轮里用已有的读工具查今明两天的日程与到期的任务、今天该看的待跟进，把要留意的记成待汇报，随摘要发出。没有就不记。平台不为此调用飞书。
- **每周例行**：在所有者工作日里的最后一天、当天最后一个摘要时点之前 60 分钟生成一条提醒；助理把未结的待跟进过一遍，按四类各记一条汇报。
- 这几种条目都是攒批级别，受每日轮次预算约束；预算用完留到次日。
```

设置相关的章节（第九节「设置」分区与第五节「上下班」里列设置项的地方）补上重点人。

- [ ] **Step 2: 全量检查**

Run: `uv run ruff check . && uv run mypy coreman`，再跑全量测试（并行方式与数据库用法同前两份计划）。
Expected: 全部通过

- [ ] **Step 3: 提交**

```bash
git add docs/superpowers/specs/2026-11-11-personal-assistant.md
git commit -m "个人助理 spec：所有者自己说过的话、重点人、每日与每周例行"
```

---

## 不在本计划内

- 起草前看表情（需要新增读表情的工具与应用权限）。
- 「我的助理」页上编辑重点人的入口（先由私聊里说）。
- 平台直接读日历、任务并渲染进摘要卡片。
- 每周体检知识库。

## 需真机确认

- 所有者授权里是否带日历与任务的读权限；没有时每日例行只汇报待跟进。
- 以用户身份读历史时，所有者自己发的富文本（`post`）消息的文本抽取是否完整。
