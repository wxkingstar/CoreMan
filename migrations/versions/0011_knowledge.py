"""M5 技能目录、审批安装与记忆元数据。静态 DDL。"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE skill_sources (
        id UUID NOT NULL,
        key TEXT NOT NULL,
        label TEXT NOT NULL,
        git_url TEXT,
        categories JSONB DEFAULT '{}'::jsonb NOT NULL,
        sort_order INTEGER DEFAULT 0 NOT NULL,
        version INTEGER DEFAULT 1 NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_skill_sources PRIMARY KEY (id),
        CONSTRAINT uq_skill_sources_key UNIQUE (key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE env_presets (
        group_key TEXT NOT NULL,
        label TEXT NOT NULL,
        vars_enc TEXT NOT NULL,
        tags TEXT[] DEFAULT '{}' NOT NULL,
        version INTEGER DEFAULT 1 NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_env_presets PRIMARY KEY (group_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE skills (
        id UUID NOT NULL,
        name TEXT NOT NULL,
        source_id UUID NOT NULL,
        description TEXT DEFAULT '' NOT NULL,
        category TEXT,
        security_level TEXT DEFAULT 'public' NOT NULL,
        version TEXT,
        env_groups TEXT[] DEFAULT '{}' NOT NULL,
        selectable_env_groups JSONB DEFAULT '{}'::jsonb NOT NULL,
        data_sources JSONB,
        user_env_vars JSONB DEFAULT '{}'::jsonb NOT NULL,
        install_type TEXT DEFAULT 'git' NOT NULL,
        external_repo_url TEXT,
        mcp_config_enc TEXT,
        security_prompt_template TEXT,
        enabled BOOLEAN DEFAULT false NOT NULL,
        revision INTEGER DEFAULT 1 NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_skills PRIMARY KEY (id),
        CONSTRAINT ck_skills_security_level CHECK (security_level IN ('public','internal')),
        CONSTRAINT ck_skills_install_type CHECK (install_type IN ('git','mcp')),
        CONSTRAINT uq_skills_name UNIQUE (name),
        CONSTRAINT fk_skills_source_id_skill_sources FOREIGN KEY(source_id) REFERENCES
        skill_sources (id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX skills_source_idx ON skills (source_id)
        """
    )
    op.execute(
        """
        CREATE TABLE bot_skills (
        bot_id UUID NOT NULL,
        skill_id UUID NOT NULL,
        status TEXT NOT NULL,
        version TEXT,
        installed_at TIMESTAMP WITH TIME ZONE,
        selected_env_groups TEXT[] DEFAULT '{}' NOT NULL,
        user_env_vars_enc TEXT,
        security_prompt TEXT,
        approved_databases TEXT[],
        approved_by UUID,
        approved_at TIMESTAMP WITH TIME ZONE,
        install_task_id BIGINT,
        error_message TEXT,
        revision INTEGER DEFAULT 1 NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_bot_skills PRIMARY KEY (bot_id, skill_id),
        CONSTRAINT ck_bot_skills_status CHECK (status IN
        ('installed','pending_approval','installing','failed','uninstalled')),
        CONSTRAINT fk_bot_skills_bot_id_bots FOREIGN KEY(bot_id) REFERENCES bots (id) ON DELETE
        CASCADE,
        CONSTRAINT fk_bot_skills_skill_id_skills FOREIGN KEY(skill_id) REFERENCES skills (id),
        CONSTRAINT fk_bot_skills_install_task_id_tasks FOREIGN KEY(install_task_id) REFERENCES
        tasks (id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX bot_skills_skill_idx ON bot_skills (skill_id)
        """
    )
    op.execute(
        """
        CREATE TABLE skill_approvals (
        id UUID NOT NULL,
        bot_id UUID NOT NULL,
        skill_id UUID NOT NULL,
        requested_databases TEXT[] DEFAULT '{}' NOT NULL,
        requested_security_prompt TEXT NOT NULL,
        reinstall_code BOOLEAN DEFAULT false NOT NULL,
        skill_revision INTEGER NOT NULL,
        approved_databases TEXT[],
        approved_security_prompt TEXT,
        status TEXT DEFAULT 'pending' NOT NULL,
        requested_by UUID NOT NULL,
        requested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        reviewed_by UUID,
        reviewed_at TIMESTAMP WITH TIME ZONE,
        review_comment TEXT,
        version INTEGER DEFAULT 1 NOT NULL,
        CONSTRAINT pk_skill_approvals PRIMARY KEY (id),
        CONSTRAINT ck_skill_approvals_status CHECK (status IN
        ('pending','approved','rejected','withdrawn')),
        CONSTRAINT fk_skill_approvals_bot_id_bots FOREIGN KEY(bot_id) REFERENCES bots (id) ON
        DELETE CASCADE,
        CONSTRAINT fk_skill_approvals_skill_id_skills FOREIGN KEY(skill_id) REFERENCES skills
        (id),
        CONSTRAINT fk_skill_approvals_requested_by_users FOREIGN KEY(requested_by) REFERENCES
        users (id),
        CONSTRAINT fk_skill_approvals_reviewed_by_users FOREIGN KEY(reviewed_by) REFERENCES users
        (id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX skill_approvals_one_pending ON skill_approvals (bot_id, skill_id)
        WHERE status = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX skill_approvals_pending_idx ON skill_approvals (requested_at) WHERE status =
        'pending'
        """
    )
    op.execute(
        """
        CREATE TABLE memories (
        id UUID NOT NULL,
        bot_id UUID NOT NULL,
        file_name TEXT NOT NULL,
        name TEXT,
        description TEXT,
        type TEXT,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        collected_from TEXT,
        file_mtime TIMESTAMP WITH TIME ZONE,
        deleted_at TIMESTAMP WITH TIME ZONE,
        version INTEGER DEFAULT 1 NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_memories PRIMARY KEY (id),
        CONSTRAINT uq_memories_bot_id UNIQUE (bot_id, file_name),
        CONSTRAINT fk_memories_bot_id_bots FOREIGN KEY(bot_id) REFERENCES bots (id) ON DELETE
        CASCADE
        )
        """
    )


def downgrade() -> None:
    op.drop_table("memories")
    op.drop_table("skill_approvals")
    op.drop_table("bot_skills")
    op.drop_table("skills")
    op.drop_table("env_presets")
    op.drop_table("skill_sources")
