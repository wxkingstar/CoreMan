export interface Page<T> { items: T[]; total: number; page: number; per_page: number }
export type Role = 'platform_admin' | 'ai_committee' | 'team_lead' | 'member'
export type Platform = 'wecom' | 'feishu'

export interface UserOut {
  id: string; login_name: string | null; display_name: string; email: string | null; mobile: string | null
  avatar_url: string | null; status: 'active' | 'disabled'; locale: 'zh' | 'ja' | 'en'; role: Role
  source: 'sync' | 'bootstrap' | 'manual'; team_id: string | null; team_name: string | null
  position: string | null; skills: string | null; bot_accessible: boolean; manual_fields: string[]
  last_login_at: string | null; identities: { platform: Platform; platform_user_id: string }[]; departments: string[]
}
export type UserPatch = Partial<Pick<UserOut, 'team_id' | 'role' | 'locale' | 'bot_accessible' | 'position' | 'skills' | 'status'>>
export interface RuleIn { platform: Platform | null; dept_path_contains: string; sort_order: number }
export interface TeamOut {
  id: string; slug: string; name_zh: string; name_ja: string | null; name_en: string | null
  sort_order: number; enabled: boolean; member_count: number; rules: (RuleIn & { id: string })[]
}
export type TeamIn = Omit<TeamOut, 'id' | 'member_count' | 'rules'>
export interface DeptNode { id: string; platform_dept_id: string; name: string; path: string; sort_order: number; member_count: number; children: DeptNode[] }
export interface PlatformAppOut {
  id: string; platform: Platform; name: string; capabilities: string[]; corp_id: string | null; app_id: string | null
  callback_url?: string | null
  secret: string; callback_token: string | null; callback_aes_key: string | null; extra: Record<string, unknown>
  enabled: boolean; version: number; created_at: string; updated_at: string
}
export type PlatformAppIn = Omit<PlatformAppOut, 'id' | 'version' | 'created_at' | 'updated_at' | 'callback_url'>
export interface SyncRun { id: number; status: 'running' | 'success' | 'failed' | 'aborted'; started_at: string; finished_at: string | null; stats: Record<string, unknown>; error: string | null; triggered_by: string | null }
export interface Providers { wecom: boolean; feishu: boolean }

// ---- M1b：relay 实例、模型目录、机器人、审计与设置 ----

export type HealthStatus = 'healthy' | 'down' | 'auth_fail' | 'timeout' | 'unknown'
export type RuntimeEnv = 'host' | 'chroot' | 'nspawn'
export type ModelsMode = 'inherit' | 'restricted'
export type Visibility = 'all' | 'admins'
export type Backend = 'claude' | 'codex'
export type EffortLevel = 'low' | 'medium' | 'high' | 'xhigh'
/** 限额百分比：后端是 Numeric 列，序列化后可能是数字也可能是数字字符串，取值一律过 Number()。 */
export type Pct = number | string | null

export interface RelayOut {
  runtime_node_id?: string | null; runtime_name?: string | null; workspace_root?: string | null
  id: string; name: string; host: string; clawrelay_port: number; agent_port: number | null
  relay_url: string; ssh_user: string | null; runtime_env: RuntimeEnv
  chroot_path: string | null; runtime_user: string | null
  model_provider: string; supported_models_mode: ModelsMode; supported_models: string[] | null
  effective_models: string[]; default_model: string | null
  team_id: string | null; team_name: string | null; visibility: Visibility
  description: string | null; is_active: boolean; has_agent_token: boolean
  rate_limit_5h_used_pct: Pct; rate_limit_5h_resets_at: string | null
  rate_limit_7d_used_pct: Pct; rate_limit_7d_resets_at: string | null
  rate_limit_probed_at: string | null
  health_status: HealthStatus; health_checked_at: string | null; health_detail: string | null
  health_latency_ms: number | null; health_fail_count: number
  relay_version: string | null; relay_mode: string | null
  bot_count: number; version: number; created_at: string; updated_at: string
}
export type RelayIn = Pick<
  RelayOut,
  'name' | 'host' | 'clawrelay_port' | 'agent_port' | 'ssh_user' | 'runtime_env' | 'chroot_path'
  | 'runtime_user' | 'model_provider' | 'supported_models_mode' | 'supported_models' | 'team_id'
  | 'visibility' | 'description' | 'is_active'
>
export interface RelayModels { provider: string; mode: ModelsMode; models: string[]; default: string | null }
export interface RelayHealth {
  status: HealthStatus; detail: string | null; latency_ms: number
  backend: string | null; version: string | null; mode: string | null
}
export interface ProbeResult { health: RelayHealth; added_models: string[]; relay: RelayOut }
export interface TeamLoadRow { team_id: string; team_name: string; relay_count: number; relay_names: string[]; bot_count: number }
export interface TeamLoad { teams: TeamLoadRow[]; unassigned_user_count: number; unassigned_bot_count: number }

export interface CatalogOut {
  provider: string; model: string; display_name: string | null; is_default: boolean
  retired: boolean; supports_xhigh: boolean; sort_order: number; backend: Backend
}
export type CatalogIn = Omit<CatalogOut, 'backend'>
export type CatalogPatch = Partial<Omit<CatalogOut, 'provider' | 'model' | 'backend'>>

export interface BotPermissions {
  role: 'creator' | 'admin' | null
  can_view_sensitive: boolean; can_view_env_full: boolean; can_edit: boolean
  can_switch_relay: boolean; can_toggle: boolean; can_delete: boolean
  can_manage_members: boolean; can_reassign_team: boolean
}
export interface BotOut {
  id: string; bot_key: string; platform: Platform; name: string; description: string
  avatar_url: string | null; enabled: boolean
  team_id: string | null; team_name: string | null
  created_by: string; created_by_name: string | null
  relay_server_id: string | null; relay_name: string | null; relay_url: string | null
  model: string; backend: Backend; working_dir: string
  verbosity_level: number; effort_level: EffortLevel | null
  sse_timeout_seconds: number
  welcome_message: string | null
  member_count: number; allowed_user_count: number
  permissions: BotPermissions; version: number; created_at: string; updated_at: string
  // 敏感字段：只有 permissions.can_view_sensitive / can_view_env_full 时后端才下发。
  // notify_webhook_url 也在内（企微群机器人的 key 就在 URL 里），且只回脱敏值。
  system_prompt?: string
  notify_webhook_url?: string | null
  credentials?: Record<string, string>
  env_vars?: Record<string, string>
  env_vars_full?: Record<string, string>
}
export interface BotIn {
  bot_key: string; platform: Platform; name: string; description: string
  avatar_url: string | null; team_id: string | null; relay_server_id: string | null
  model: string; working_dir: string; system_prompt: string
  verbosity_level: number; effort_level: EffortLevel | null
  sse_timeout_seconds: number
  credentials: Record<string, string>; env_vars: Record<string, string>
  welcome_message: string | null
  enabled: boolean
}
export type BotPatch = Partial<Omit<BotIn, 'bot_key' | 'platform' | 'enabled'>>
export interface SwitchRelayIn { relay_server_id: string; model?: string | null }
export interface SwitchRelayOut {
  old_relay_id: string | null; new_relay_id: string
  old_model: string; new_model: string; bot: BotOut
}
export interface BotMemberOut { user_id: string; login_name: string | null; display_name: string; added_at: string; added_by: string | null }
export interface AllowedUserOut { user_id: string; login_name: string | null; display_name: string }

export interface AuditLogOut {
  id: number; actor_id: string | null; actor_login: string | null; action: string
  target_type: string | null; target_id: string | null
  diff: Record<string, unknown> | null; ip: string | null; created_at: string
}

/** 提示词段落键的短名，顺序即 `coreman/core/prompting/defaults.py::PROMPT_DEFAULTS_BY_KEY`。 */
export const PROMPT_SEGMENTS = [
  'security_policy', 'codex_contract', 'runtime_mode', 'runtime_tail',
  'verbosity_2', 'verbosity_3', 'verbosity_4',
] as const
export type PromptSegment = (typeof PROMPT_SEGMENTS)[number]
export type PromptSettings = { [K in PromptSegment as `prompt_${K}`]: string }

export interface SettingsOut extends PromptSettings {
  bootstrap_admin_enabled: boolean
  default_model: string
  default_verbosity_level: number
  default_effort_level: EffortLevel | null
  session_ttl_hours: number
  jwt_issuer: string
  /** 全局并发闸门（spec §6.3）：worker 同时在跑的任务上限，其中 fast 车道独占的名额。 */
  max_concurrent_tasks: number
  fast_lane_slots: number
  /** 企微模板卡片左上角的来源图标；空串表示不显示图标。 */
  card_icon_url: string
}
export type SettingsPatch = Partial<SettingsOut>
export type SettingsDefaults = Pick<SettingsOut, 'default_model' | 'default_verbosity_level' | 'default_effort_level'>

// ---- M2：对话记录、运行状态、并发与提示词设置 ----

/** 对话记录状态（`coreman/core/db/models/logs.py::CHAT_LOG_STATUSES`）。 */
export type ChatLogStatus = 'success' | 'error' | 'timeout' | 'stopped' | 'ask_user' | 'failed'
/** 会话类型（同上 `CHAT_TYPES`）。 */
export type ChatType = 'single' | 'group' | 'cron'

export interface ChatLogOut {
  id: number; bot_id: string; bot_key: string; platform: string
  user_id: string | null; user_login: string | null; user_name: string | null
  chat_type: ChatType; chat_id: string | null; session_key: string | null
  relay_session_id: string | null; model: string | null; stream_id: string | null; task_id: number | null
  message_type: string; message_preview: string | null; response_preview: string | null
  tools_used: string[]; status: ChatLogStatus; error_code: string | null; latency_ms: number | null
  input_tokens: number | null; output_tokens: number | null
  cache_read_tokens: number | null; cache_creation_tokens: number | null
  request_at: string; response_at: string | null
  // 只有详情端点 `GET /api/admin/chat-logs/{id}` 才下发的全文字段；列表项里一律 undefined。
  message_content?: string | null
  quoted_content?: string | null
  file_info?: Record<string, unknown> | null
  response_content?: string | null
  error_message?: string | null
  relay_server_id?: string | null
  cost_usd?: number | null
}
export interface ChatLogByBot {
  bot_id: string; bot_key: string; total: number
  /** 后端口径：`status IN (error, timeout, failed)` 的合计，不是只数 error。 */
  success: number; error: number; avg_latency_ms: number | null
}
export interface ChatLogStats {
  total: number
  /** 只含出现过的状态。 */
  by_status: Record<string, number>
  avg_latency_ms: number | null
  tokens: { input: number; output: number; cache_read: number; cache_creation: number }
  by_bot: ChatLogByBot[]
}

export interface RuntimeInstance {
  id: string; service: string; version: string; started_at: string; heartbeat_at: string
  /** 后端算好的活体判定（60 秒内有心跳）。 */
  alive: boolean
  capacity: number | null; running: number
  drain_requested_at: string | null; stopped_at: string | null
}
/** 租约连接态（`coreman/core/db/models/bus.py::CONNECTION_STATES`）。 */
export type ConnectionState = 'disconnected' | 'connecting' | 'subscribed' | 'kicked' | 'auth_failed'
export interface RuntimeLease {
  bot_id: string; bot_key: string; bot_name: string; platform: string
  holder_instance: string | null; generation: number; connection_state: ConnectionState
  acquired_at: string | null; heartbeat_at: string | null
  drain_requested_by: string | null; released_at: string | null
}
export interface RuntimeQueue {
  queued: { normal: number; fast: number }
  claimed: number; running: number
  outbox_pending: number; outbox_failed: number; streams_active: number
}
/** 任务状态（同上 `TASK_STATUSES`）。 */
export type TaskStatus = 'queued' | 'claimed' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'timed_out'
export interface RuntimeTask {
  id: number; bot_id: string; bot_key: string | null; kind: string; lane: string; priority: number
  session_key: string | null; status: TaskStatus; claimed_by: string | null; run_after: string
  claimed_at: string | null; started_at: string | null; heartbeat_at: string | null
  cancel_requested_at: string | null; cancel_reason: string | null; attempts: number
}
export interface RuntimeOutboxItem {
  id: number; bot_id: string | null; bot_key: string | null; kind: string; dedupe_key: string; status: string
  attempts: number; not_before: string; last_error: string | null; created_at: string; sent_at: string | null
}
/** 排空目标：实例与机器人严格二选一，两个都给或都不给后端返回 422。 */
export interface DrainIn { instance_id?: string; bot_key?: string }

// ---- M3a：公告 ----

/** 公告范围（`coreman/api/routers/announcements.py::Scope`）。 */
export type AnnouncementScope = 'global' | 'relay' | 'bot'
/** 只看时间窗推导，不看 `is_active`：「停用了但还在窗口里」与「启用了但还没到点」是两回事。 */
export type AnnouncementTimeStatus = 'pending' | 'active' | 'expired'

export interface AnnouncementOut {
  id: string; scope: AnnouncementScope
  relay_server_id: string | null; relay_name: string | null
  bot_id: string | null; bot_key: string | null; bot_name: string | null
  content: string; is_active: boolean
  start_at: string | null; end_at: string | null
  time_status: AnnouncementTimeStatus
  created_by: string | null; created_at: string; updated_at: string
}
/** 范围与目标必须匹配（global 两个都为空、relay 只给 relay_server_id、bot 只给 bot_id），否则后端 422。 */
export interface AnnouncementIn {
  scope: AnnouncementScope
  relay_server_id?: string | null
  bot_id?: string | null
  content: string
  is_active: boolean
  start_at: string | null
  end_at: string | null
}
