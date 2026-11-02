"""Seed the OpenCode backend's model catalog with OpenCode Go models.

- 新后端 opencode：节点装了 OpenCode CLI 后，心跳会补建它的 relay，驱动上报的模型也会自动进目录；
  这里先放几种 OpenCode Go 套餐里的常用模型，并把 Kimi K3 设为默认，平台默认模型与换运行时
  才有可落的模型。
- 模型名是 `opencode/<服务商>/<模型>`，驱动调 CLI 前去掉开头的 `opencode/`。
- 已有同名行不动；opencode 下已有默认模型时不改默认。
"""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

PROVIDER = "opencode"
DEFAULT = "opencode/opencode-go/kimi-k3"
MODELS = (
    ("opencode/opencode-go/kimi-k3", "Kimi K3 (OpenCode Go)"),
    ("opencode/opencode-go/glm-5.3", "GLM-5.3 (OpenCode Go)"),
    ("opencode/opencode-go/deepseek-v4-pro", "DeepSeek V4 Pro (OpenCode Go)"),
    ("opencode/opencode-go/deepseek-v4.1-flash", "DeepSeek V4.1 Flash (OpenCode Go)"),
    ("opencode/opencode-go/qwen3.8-max", "Qwen3.8 Max (OpenCode Go)"),
    ("opencode/opencode-go/minimax-m3", "MiniMax M3 (OpenCode Go)"),
)


def upgrade() -> None:
    connection = op.get_bind()
    for index, (model, display_name) in enumerate(MODELS):
        connection.execute(
            sa.text(
                "INSERT INTO model_catalog (provider, model, display_name, sort_order) "
                "VALUES (:provider, :model, :display_name, :sort_order) "
                "ON CONFLICT (provider, model) DO NOTHING"
            ),
            {
                "provider": PROVIDER,
                "model": model,
                "display_name": display_name,
                "sort_order": (len(MODELS) - index) * 10,
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
