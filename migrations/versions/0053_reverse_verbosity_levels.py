"""Reverse the response-detail levels so 1 is the tersest and 4 is the model's own default.

- 旧含义：1 不加说明（模型默认）、2 精简、3 像同事聊天、4 极简；管理台却标成 1 极简 … 4 详细。
  新含义与标签一致：1 极简、2 简洁、3 标准、4 详细（不加说明，即模型默认）。
- 每个机器人行为不变：档位 n 改为 5 − n（旧 1 → 4 …），version+1 并发 config_changed。
  列默认值与新建表单的平台默认从 1 改为 4。
- 设置里改写过的档位提示词跟着档位走（prompt_verbosity_4 → _1、_3 → _2、_2 → _3）；
  仍等于旧出厂文案的删掉，让新出厂文案生效。

滚动升级期间，尚未替换的旧 worker 会按旧含义读新档位（用默认档的机器人短暂得到旧 4 档「极简」），
worker 全部换新后恢复。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

OLD_FACTORY = {
    2: "Give a concise answer with the result and essential supporting details.",
    3: "Use plain, natural language. Present conclusions without private reasoning.",
    4: "Keep the answer minimal, but include the result and any required user action.",
}
NEW_FACTORY = {
    1: """# Response Length: Minimal

Reply with the bare answer and nothing else. This overrides any other guidance about length or \
format.

- A yes-or-no question gets only yes or no in the user's language (for example "是。" or \
"不是。"), with no qualification.
- A question about a fact gets only the fact: a number, a name, a date or a short phrase.
- When the user asks for specific data or a deliverable, give exactly that in the most compact \
form, without commentary.
- Any other request, including one for an explanation, a comparison or a complete plan, gets only \
the core conclusion in one or two short sentences (about 50 Chinese characters or 30 English \
words).

No reasons, nuances, caveats, examples, background, headings, tables or offers of help. After \
doing a task, report only the outcome and anything the user must do.""",
    2: """# Response Length: Brief

Talk like a person of few words: plain spoken sentences, usually one to three and at most about \
100 Chinese characters or 60 English words, even when asked to explain or plan. Answer directly \
and add a reason only when the answer would be unclear without it. No headings, lists, tables, \
caveats or recaps. This overrides any other guidance about length or format.""",
    3: """# Response Length: Standard

Give the answer first, then a short explanation of the key points: one paragraph or three to five \
bullets, at most about 400 Chinese characters or 250 English words. For a large request such as \
a complete plan, give only the main points, not every section or detail; the user can ask for \
more. Leave out background, rare edge cases and repetition. This overrides any other guidance \
about length.""",
}


def _reverse(default: int, factory: dict[int, str]) -> None:
    """档位 n ↔ 5 − n；factory 是迁移前的出厂文案，等于它的覆盖值不再保留。"""
    connection = op.get_bind()
    moved = connection.execute(
        sa.text(
            "UPDATE bots SET verbosity_level = 5 - verbosity_level, version = version + 1 "
            "RETURNING id"
        )
    ).scalars()
    for bot_id in moved:
        connection.execute(
            sa.text("SELECT pg_notify('config_changed', :payload)"),
            {"payload": json.dumps({"table": "bots", "id": str(bot_id)})},
        )
    op.alter_column("bots", "verbosity_level", server_default=sa.text(str(default)))
    connection.execute(
        sa.text(
            "UPDATE settings SET value = to_jsonb(5 - (value #>> '{}')::int) "
            "WHERE key = 'default_verbosity_level' AND jsonb_typeof(value) = 'number'"
        )
    )
    keys = [f"prompt_verbosity_{n}" for n in factory]
    rows = connection.execute(
        sa.text("SELECT key, value, updated_by FROM settings WHERE key = ANY(:keys)"),
        {"keys": keys},
    ).all()
    connection.execute(sa.text("DELETE FROM settings WHERE key = ANY(:keys)"), {"keys": keys})
    for key, value, updated_by in rows:
        level = int(key.rsplit("_", 1)[1])
        if value == factory[level]:
            continue
        connection.execute(
            sa.text(
                "INSERT INTO settings (key, value, updated_by) "
                "VALUES (:key, CAST(:value AS jsonb), :updated_by)"
            ),
            {
                "key": f"prompt_verbosity_{5 - level}",
                "value": json.dumps(value),
                "updated_by": updated_by,
            },
        )


def upgrade() -> None:
    _reverse(4, OLD_FACTORY)


def downgrade() -> None:
    _reverse(1, NEW_FACTORY)
