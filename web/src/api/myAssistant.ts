import { ApiError, call, http } from '@/api/client'

/** 「我的助理」页的接口：只对所有者本人，助理由登录人推出，路径里没有助理编号。 */

export interface AssistantFlow { action: string; step: string }

/** 没有助理时的空状态：能不能开通、不能的原因代号、怎么开通。 */
export interface AssistantAbsent {
  exists: false
  eligible: boolean
  reason: 'inactive' | 'no_login_name' | 'not_listed' | null
  how_to: { app_name: string | null; steps: string[] }
  lifecycle: AssistantFlow | null
}

export interface AssistantOverview {
  exists: true
  bot_id: string
  name: string
  state: 'provisioning' | 'active' | 'deprovisioning' | 'deprovisioned' | null
  runtime: { name: string; online: boolean } | null
  /** 可用状态下现在跑不了轮次的原因代码；能跑是 null。 */
  paused_reason: string | null
  on_shift: boolean
  /** 全天候（默认）；false 是只在工作时段里在班。 */
  always_on: boolean
  /** 还没过期的「上班」「下班」临时切换；按工作时段时是 null。 */
  shift_override: 'on' | 'off' | null
  shift_override_until: string | null
  cold_start: { state: 'pending' | 'in_progress' | 'done'; step: string | null; outcome: string | null }
  turns: { today: number; budget: number }
  watch: { state: string; last_ok_at: string | null; last_error: string | null } | null
  lifecycle: AssistantFlow | null
  /** 数据密钥已不可用：内容列都读不出来，页面只显示元数据。 */
  content_locked: boolean
  approval_system: { name: string | null; url: string | null }
}

export type FollowupCategory = 'decide' | 'promised' | 'waiting' | 'remind'
export const FOLLOWUP_CATEGORIES: readonly FollowupCategory[] = ['decide', 'promised', 'waiting', 'remind']

export interface Followup {
  id: string
  category: FollowupCategory
  status: 'open' | 'snoozed' | 'done' | 'dropped'
  title: string
  notes?: string | null
  review_on: string | null
  due_on: string | null
  origin_message_id: string | null
  created_at: string
  due_today?: boolean
  close_code?: string
  locked?: boolean
}
export interface FollowupList { today: string; groups: Record<FollowupCategory, Followup[]>; total: number }
export interface FollowupIn {
  category: FollowupCategory
  title: string
  notes?: string | null
  review_on?: string | null
  due_on?: string | null
}
export type FollowupPatch = Partial<FollowupIn>

export interface AssistantJob {
  id: string
  kind: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'blocked'
  title: string
  rounds: number
  created_at: string
  finished_at: string | null
  has_result: boolean
  error_code?: string
  brief?: string
  result?: string | null
  blocked_reason?: string | null
  locked?: boolean
  /** `system` 是平台派的（下班整理），与所有者让助理派的分开显示。 */
  origin?: 'assistant' | 'system'
  /** 调度公共员工（`kind` 为 `bot_help`）的目标；员工已删除时名字是 null。 */
  target?: { id: string; name: string | null } | null
}
export interface JobList { active: AssistantJob[]; recent: AssistantJob[] }

/** 等所有者批的一张单：正文在审批系统里看，这里只有摘要与入口。 */
export interface PendingApproval { id: string; tool: string; summary: string | null; created_at: string; link: string | null }
export interface SettledApproval {
  id: string
  tool: string
  channel: string
  status: 'executed' | 'rejected' | 'withdrawn' | 'failed' | 'expired' | 'stale'
  summary: string | null
  reason: string | null
  error: string | null
  created_at: string
  decided_at: string | null
  executed_at: string | null
  locked: boolean
}
export interface Attention { today: string; approvals: PendingApproval[]; jobs: AssistantJob[]; followups: Followup[] }

export interface NoteEntry {
  path: string
  title: string
  summary: string
  pinned: boolean
  version: number
  size: number
  updated_by: 'owner' | 'assistant' | 'import'
  updated_at: string
}
export interface NotePage extends NoteEntry { body: string }
export interface NoteIndex { items: NoteEntry[]; locked: boolean }
/** `version` 是读到的版本，0 表示新建；`pinned` 省略表示保持原样。 */
export interface NoteIn { path: string; body: string; summary: string; title?: string | null; pinned?: boolean | null; version: number }

export interface AssistantPrefs {
  timezone: string
  /** 全天候；开着时工作时段与工作日保留但不生效。 */
  always_on: boolean
  work_start: string
  work_end: string
  workdays: number[]
  focus_chats: string[]
  ignored_chats: string[]
  /** null = 跟随平台默认。 */
  daily_turn_budget: number | null
  platform_daily_turn_budget: number
  /** 实际生效的：个人值与平台默认里较小的那个。 */
  effective_daily_turn_budget: number
  limits: { focus_chats: number; ignored_chats: number; budget_min: number }
  locked: boolean
  /** 以下几项来自后端的 `settings.view`，表单不改它们。 */
  on_shift: boolean
  shift_override: 'on' | 'off' | null
  shift_override_until: string | null
  cold_start: 'pending' | 'in_progress' | 'done'
  today: string
}
export type AssistantPrefsPatch = Partial<Pick<AssistantPrefs,
  'timezone' | 'always_on' | 'work_start' | 'work_end' | 'workdays' | 'focus_chats' | 'ignored_chats' | 'daily_turn_budget'>>

/** 访问记录的一行：谁、什么时候、做了什么；没有任何内容。 */
export interface AccessEntry {
  id: number
  at: string
  actor_kind: 'owner' | 'admin' | 'system'
  /** 操作人的显示名；管理命令或账号已删除时为 null。 */
  actor_name: string | null
  action: string
  target: string
}
/** `next_before` 不为 null 时还有更早的，带着它再取下一页。 */
export interface AccessPage { items: AccessEntry[]; next_before: number | null }

/** 导入清单里的一行：只有路径、是否置顶或跳过的原因代码，没有正文。 */
export interface ImportLine { path: string; pinned?: boolean; reason?: string }
/** 导入的清单。确认时把 `digest` 原样带回去：期间库里有变化，服务端会拒绝并要求重新预览。 */
export interface ImportPlan {
  dry_run: boolean
  digest: string
  /** 包里跳过没读的系统文件个数（.DS_Store 之类）。 */
  ignored: number
  create: ImportLine[]
  overwrite: ImportLine[]
  skip: ImportLine[]
  followups: { create: number; skip: { index: number; reason: string }[] }
}

/** 导入与导出要解压、逐页写入或打包，比普通接口慢得多。 */
const TRANSFER_TIMEOUT = 180_000

const root = '/api/me/assistant'
/** 等待审批的调度靠这个工具名认出来（它没有子任务行）。 */
export const DISPATCH_TOOL = 'assistant_ask_bot'
// 这几个请求可能要等后台：改设置时把会话移出忽略名单要等正在进行的一轮轮询提交（上限 30 秒），
// 重新生成口吻页要以所有者身份读几个会话的近期消息。
const SLOW = { timeout: 60_000 }
export const myAssistant = {
  overview: () => call<AssistantOverview | AssistantAbsent>(http.get(root)),
  attention: () => call<Attention>(http.get(`${root}/attention`)),
  jobs: () => call<JobList>(http.get(`${root}/jobs`)),
  cancelJob: (id: string) => call<{ id: string; status: string }>(http.post(`${root}/jobs/${id}/cancel`)),
  followups: () => call<FollowupList>(http.get(`${root}/followups`)),
  addFollowup: (body: FollowupIn) => call<Followup>(http.post(`${root}/followups`, body)),
  updateFollowup: (id: string, body: FollowupPatch) => call<Followup>(http.patch(`${root}/followups/${id}`, body)),
  closeFollowup: (id: string, dropped = false) => call<Followup>(http.post(`${root}/followups/${id}/close`, { dropped })),
  snoozeFollowup: (id: string, until: string) => call<Followup>(http.post(`${root}/followups/${id}/snooze`, { until })),
  recentApprovals: () => call<{ items: SettledApproval[] }>(http.get(`${root}/approvals/recent`)),
  notes: () => call<NoteIndex>(http.get(`${root}/notes`)),
  // 路径本身是个人内容：读与删都放请求体，不进网址（网址会进访问日志）。
  page: (path: string) => call<NotePage>(http.post(`${root}/notes/read`, { path })),
  savePage: (body: NoteIn) => call<NoteEntry>(http.put(`${root}/notes/page`, body)),
  deletePage: (path: string) => call<{ deleted: boolean }>(http.post(`${root}/notes/delete`, { path })),
  settings: () => call<AssistantPrefs>(http.get(`${root}/settings`)),
  saveSettings: (body: AssistantPrefsPatch) => call<AssistantPrefs>(http.patch(`${root}/settings`, body, SLOW)),
  /** 即时上班、下班，或回到按工作时段；与私聊里说「上班」「下班」同一个效果。 */
  shift: (state: 'on' | 'off' | 'auto') => call<AssistantPrefs>(http.post(`${root}/shift`, { state })),
  /** 重新生成口吻页：冷启动做完之后才能用，每天至多 3 次。 */
  regenerateVoice: () => call<{ queued: boolean; left_today: number }>(http.post(`${root}/voice/regenerate`, undefined, SLOW)),
  /** 停用不可撤销：调用前必须已经让所有者二次确认。 */
  deprovision: () => call<{ state: string }>(http.post(`${root}/deprovision`, { confirm: true })),
  accessLog: (before?: number) => call<AccessPage>(http.get(`${root}/access-log`, { params: before ? { before } : {} })),
  /** `dryRun` 为真只出清单；为假时必须带上预览给的 `expect`。 */
  importArchive: (file: File, options: { dryRun: boolean; overwrite: boolean; expect?: string }) => {
    const form = new FormData()
    form.append('archive', file)
    form.append('dry_run', String(options.dryRun))
    form.append('overwrite', String(options.overwrite))
    if (options.expect) form.append('expect', options.expect)
    return call<ImportPlan>(http.post(`${root}/import`, form, { timeout: TRANSFER_TIMEOUT }))
  },
  /** 下载的是二进制：失败时响应体不是 JSON 信封，只带状态码。 */
  exportArchive: async (): Promise<Blob> => {
    try {
      return (await http.get<Blob>(`${root}/export`, { responseType: 'blob', timeout: TRANSFER_TIMEOUT })).data
    } catch (e) {
      const err = e as { response?: { status?: number }; message?: string }
      const status = err.response?.status ?? 0
      throw new ApiError(status, status, err.message ?? '')
    }
  },
}
