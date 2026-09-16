"""定时任务、执行历史和人工求助。静态 DDL，不依赖运行期模型。"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE cron_jobs (
        id UUID NOT NULL,
        bot_id UUID NOT NULL,
        name TEXT NOT NULL,
        cron_expression TEXT NOT NULL,
        timezone TEXT DEFAULT 'Asia/Shanghai' NOT NULL,
        prompt TEXT NOT NULL,
        system_prompt TEXT,
        precheck_script TEXT,
        precheck_timeout_seconds INTEGER DEFAULT 30 NOT NULL,
        enabled BOOLEAN DEFAULT true NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE,
        target_users UUID[] DEFAULT '{}' NOT NULL,
        target_chats TEXT[] DEFAULT '{}' NOT NULL,
        notify_emails TEXT[] DEFAULT '{}' NOT NULL,
        notify_webhook BOOLEAN DEFAULT true NOT NULL,
        created_by UUID NOT NULL,
        next_run_at TIMESTAMP WITH TIME ZONE,
        force_run_at TIMESTAMP WITH TIME ZONE,
        force_run_by UUID,
        running_task_id BIGINT,
        last_run_at TIMESTAMP WITH TIME ZONE,
        last_status TEXT,
        version INTEGER DEFAULT 1 NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_cron_jobs PRIMARY KEY (id),
        CONSTRAINT ck_cron_jobs_precheck_timeout CHECK (precheck_timeout_seconds BETWEEN 5
        AND 120),
        CONSTRAINT fk_cron_jobs_bot_id_bots FOREIGN KEY(bot_id) REFERENCES bots (id) ON
        DELETE CASCADE,
        CONSTRAINT fk_cron_jobs_created_by_users FOREIGN KEY(created_by) REFERENCES users
        (id),
        CONSTRAINT fk_cron_jobs_force_run_by_users FOREIGN KEY(force_run_by) REFERENCES
        users (id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX cron_jobs_due_idx ON cron_jobs (next_run_at) WHERE enabled = true
        """
    )
    op.execute(
        """
        CREATE TABLE cron_runs (
        id BIGINT GENERATED ALWAYS AS IDENTITY,
        cron_job_id UUID,
        bot_id UUID NOT NULL,
        job_name TEXT NOT NULL,
        task_id BIGINT,
        executed_by UUID,
        trigger_kind TEXT DEFAULT 'scheduled' NOT NULL,
        status TEXT NOT NULL,
        prompt TEXT NOT NULL,
        reply TEXT,
        error_message TEXT,
        precheck_meta JSONB,
        delivery JSONB DEFAULT '{}'::jsonb NOT NULL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_tokens INTEGER,
        cache_creation_tokens INTEGER,
        cost_usd NUMERIC(12, 6),
        started_at TIMESTAMP WITH TIME ZONE NOT NULL,
        finished_at TIMESTAMP WITH TIME ZONE,
        CONSTRAINT pk_cron_runs PRIMARY KEY (id),
        CONSTRAINT ck_cron_runs_status CHECK (status IN ('running', 'success', 'failed',
        'skipped', 'failed_precheck')),
        CONSTRAINT fk_cron_runs_cron_job_id_cron_jobs FOREIGN KEY(cron_job_id) REFERENCES
        cron_jobs (id) ON DELETE SET NULL,
        CONSTRAINT uq_cron_runs_task_id UNIQUE (task_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX cron_runs_job_idx ON cron_runs (cron_job_id, id DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE escalations (
        id BIGINT GENERATED ALWAYS AS IDENTITY,
        escalation_id TEXT NOT NULL,
        group_id TEXT,
        bot_id UUID NOT NULL,
        from_user_id UUID,
        to_user_id UUID NOT NULL,
        notify_platform TEXT NOT NULL,
        notify_message_id TEXT,
        platform_app_id UUID,
        question TEXT NOT NULL,
        replies JSONB DEFAULT '[]'::jsonb NOT NULL,
        status TEXT NOT NULL,
        rounds SMALLINT DEFAULT 0 NOT NULL,
        nudge_stage SMALLINT DEFAULT 0 NOT NULL,
        resolution TEXT,
        activated_at TIMESTAMP WITH TIME ZONE,
        last_polled_at TIMESTAMP WITH TIME ZONE,
        last_reply_at TIMESTAMP WITH TIME ZONE,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_escalations PRIMARY KEY (id),
        CONSTRAINT ck_escalations_status CHECK (status IN ('pending', 'queued', 'replied',
        'completed', 'expired', 'cancelled')),
        CONSTRAINT ck_escalations_resolution CHECK (resolution IN
        ('agent','observed','offline','expired')),
        CONSTRAINT ck_escalations_rounds CHECK (rounds BETWEEN 0 AND 2),
        CONSTRAINT ck_escalations_nudge_stage CHECK (nudge_stage BETWEEN 0 AND 2),
        CONSTRAINT ck_escalations_notify_platform CHECK (notify_platform IN
        ('wecom_app','feishu_bot')),
        CONSTRAINT uq_escalations_escalation_id UNIQUE (escalation_id),
        CONSTRAINT fk_escalations_bot_id_bots FOREIGN KEY(bot_id) REFERENCES bots (id),
        CONSTRAINT fk_escalations_to_user_id_users FOREIGN KEY(to_user_id) REFERENCES
        users (id),
        CONSTRAINT fk_escalations_platform_app_id_platform_apps FOREIGN
        KEY(platform_app_id) REFERENCES platform_apps (id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX escalations_active_idx ON escalations (to_user_id, status) WHERE
        status IN ('pending','replied','queued')
        """
    )

    # 旧日志写入器曾把群聊当成私聊可达凭据，清理有明确群聊证据的记录。
    # 这类行无法安全还原，降级也不重新制造；用户重新私聊即可建立。
    op.execute("""
        DELETE FROM user_reached AS r WHERE EXISTS (
            SELECT 1 FROM chat_logs AS l
            WHERE l.bot_id = r.bot_id AND l.user_id = r.user_id
              AND l.chat_id = r.platform_chat_id AND l.chat_type = 'group'
        )
    """)


def downgrade() -> None:
    op.drop_table("escalations")
    op.drop_table("cron_runs")
    op.drop_table("cron_jobs")
