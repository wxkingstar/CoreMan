"""保留技能数据源选择和绑定审批的安装输入。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bot_skills", sa.Column("effective_env_enc", sa.Text(), nullable=True))
    op.add_column("skills", sa.Column("default_data_source", sa.Text(), nullable=True))
    op.add_column(
        "skills",
        sa.Column(
            "doris_enabled_groups", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")
        ),
    )
    # 0011 检查点尚未开放审批入口，不能为已有审批凭空推断安装输入。
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM skill_approvals)")).scalar():
        raise RuntimeError("存在没有安装输入快照的审批，请先核对")
    op.add_column("skill_approvals", sa.Column("request_inputs_enc", sa.Text(), nullable=False))


def downgrade() -> None:
    op.drop_column("bot_skills", "effective_env_enc")
    op.drop_column("skill_approvals", "request_inputs_enc")
    op.drop_column("skills", "doris_enabled_groups")
    op.drop_column("skills", "default_data_source")
