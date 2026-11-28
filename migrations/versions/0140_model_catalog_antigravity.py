"""Seed the Antigravity backend's model catalog.

- 新后端 antigravity：节点装了 Antigravity CLI（命令 agy）后，心跳会补建它的 relay，驱动上报的
  模型也会自动进目录；这里先放常用模型，并把 Gemini 4 Neon 设为默认，平台默认模型与换运行时
  才有可落的模型。
- 模型名是 `antigravity/<模型>`，不带思考档位（CLI 里的 gemini-4-neon-high 拆成模型与档位）；
  驱动调 CLI 前去掉开头的 `antigravity/`。
- xhigh 只有 Neon 与 Hydrogen 有（实测 agy 1.5.19）；CLI 没有 max 档。
- 已有同名行不动；antigravity 下已有默认模型时不改默认。
"""

import sqlalchemy as sa
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None

PROVIDER = "antigravity"
DEFAULT = "antigravity/gemini-4-neon"
# (模型, 展示名, 是否支持 xhigh)
MODELS = (
    ("antigravity/gemini-4-neon", "Gemini 4 Neon (Antigravity)", True),
    ("antigravity/gemini-4-carbon", "Gemini 4 Carbon (Antigravity)", False),
    ("antigravity/gemini-4-argon", "Gemini 4 Argon (Antigravity)", False),
    ("antigravity/gemini-4-hydrogen", "Gemini 4 Hydrogen (Antigravity)", True),
    ("antigravity/claude-opus-5-5", "Claude Opus 5.5 (Antigravity)", False),
    ("antigravity/claude-sonnet-5-5", "Claude Sonnet 5.5 (Antigravity)", False),
)


def upgrade() -> None:
    connection = op.get_bind()
    for index, (model, display_name, xhigh) in enumerate(MODELS):
        connection.execute(
            sa.text(
                "INSERT INTO model_catalog "
                "(provider, model, display_name, sort_order, supports_xhigh) "
                "VALUES (:provider, :model, :display_name, :sort_order, :xhigh) "
                "ON CONFLICT (provider, model) DO NOTHING"
            ),
            {
                "provider": PROVIDER,
                "model": model,
                "display_name": display_name,
                "sort_order": (len(MODELS) - index) * 10,
                "xhigh": xhigh,
            },
        )
    has_default = connection.execute(
        sa.text(
            "SELECT 1 FROM model_catalog WHERE provider = :provider AND is_default "
            "AND NOT retired LIMIT 1"
        ),
        {"provider": PROVIDER},
    ).scalar()
    if not has_default:
        connection.execute(
            sa.text(
                "UPDATE model_catalog SET is_default = (model = :model) WHERE provider = :provider"
            ),
            {"provider": PROVIDER, "model": DEFAULT},
        )


def downgrade() -> None:
    # 目录行可能已被机器人引用或由节点上报，降级不删，只是不再有 relay 用到。
    pass
