import { call, http } from './client'

export type WorkTaskStatus = 'shadow' | 'todo' | 'in_progress' | 'waiting' | 'review' | 'done' | 'cancelled'
export type WorkTaskView = 'needs_me' | 'created' | 'watching' | 'all'
export interface WorkTaskBot { id: string; name: string }
export interface WorkTaskActor { type: 'human' | 'bot'; id: string; name: string }
export interface WorkTaskOption { key: string; label: string }
/** 待选择事项：网页和飞书卡片上是同一份，任一处先回答即生效。 */
export interface WorkTaskAsk {
  id: string; kind: string; to: WorkTaskActor | null; question: string; why: string
  options: WorkTaskOption[]; other: boolean; needs_text: string[]; mine: boolean
  /** 被问的人自己，或代表人、平台管理员代答。 */
  can_answer: boolean
}
/** 给人看的那一层。 */
export interface WorkTask {
  code: string; title: string; summary: string; status: WorkTaskStatus; source: string
  /** 自动记下的账本记录（API 任务、求助）：只读。 */
  mirror: boolean
  platform: string | null; chat_id: string | null; requester_name: string
  requester: WorkTaskActor | null; principal: WorkTaskActor | null
  assignee: WorkTaskActor | null; reviewer: WorkTaskActor | null; dispatcher: WorkTaskBot | null
  review_required: boolean; priority: 'normal' | 'high' | 'urgent'; due_at: string | null
  start_mode: 'auto' | 'manual'; claimed_at: string | null; needs_drive: boolean
  /** 现在在谁手上、等什么：一句话。 */
  now: string
  ask: WorkTaskAsk | null; can_manage: boolean; related_codes: string[]
  /** 产出物（给人看）。 */
  artifacts: { title: string; url: string }[]
  created_at: string; updated_at: string; closed_at: string | null
  /** 列表里才有：这件事下面拆了几步、做完几步，几个要人定的决策点、还有几个没定。 */
  steps?: number; steps_done?: number; decisions?: number; decisions_open?: number
}
export interface WorkTaskEvent {
  id: number; kind: string; actor_type: 'human' | 'bot' | 'system'; actor_name: string
  human_text: string; agent_detail: Record<string, unknown>; created_at: string
  /** 给人看的里程碑；其余是 AI 员工的流水，「经过」里默认不显示。 */
  milestone: boolean
}
export interface WorkTaskRun {
  id: number; bot: WorkTaskBot | null; trigger: string; triggered_by: Record<string, unknown>
  status: string; outcome: string; session_key: string; chat_task_id: number
  started_at: string; ended_at: string | null
}
/** 全貌里的一步：一件子任务，或一个问过人的问题。 */
export interface WorkTaskStep {
  code: string; title: string; status: WorkTaskStatus
  kind: 'running' | 'waiting_human' | 'done' | 'cancelled'
  question: boolean; assignee: WorkTaskActor | null
  /** 开始时间（没领取过的是创建时间）。 */
  since: string; closed_at: string | null
  /** 后面跟的一句：做完的是结果，在等人的是等谁，在做的是最近一次进展（`note_at` 是它的时间）。 */
  note: string; note_at: string | null
  /** 牵头员工拆步骤时写的阶段；没写是空。 */
  phase: string
  /** 看着在做、其实没在动：排队太久、上一轮结束没接着做、运行心跳断了。 */
  stalled: WorkTaskStalled | null
}
export interface WorkTaskStalled { kind: 'queued' | 'idle' | 'lost'; since: string }
/** 任务页首屏的全貌，和群里的进展卡同一套分块。 */
export interface WorkTaskOverview {
  state: 'need_human' | 'running' | 'todo' | 'done' | 'cancelled' | 'shadow'
  /** 这件事自己（没拆步骤时）是不是没在动。 */
  stalled: WorkTaskStalled | null
  steps_done: number; steps_total: number
  /** 这件事自己和它下面各层还没人答的问题。 */
  asks: (WorkTaskAsk & { code: string; title: string })[]
  doing: WorkTaskStep[]; plan: string[]; done: WorkTaskStep[]
  /** 办完后还要人处理的事：每件带上办没办、谁勾的，或者建成了哪个任务。 */
  followups: { index: number; text: string; done: boolean; by: string; at: string | null; task: string }[]
  /** 这件事上上下下人定过的事：谁、什么时候、答了什么。 */
  decisions: { code: string; question: string; by: string; at: string | null; answer: string }[]
  latest: { text: string; at: string; actor_name: string } | null
}
export interface WorkTaskDetail extends WorkTask {
  watching: boolean; watchers: { id: string; name: string }[]
  parent: { code: string; title: string } | null; children: WorkTask[]
  overview: WorkTaskOverview
  /** 新建或指派时选了现在开工但没开成的原因（只在那一次的返回里有）。 */
  start_error?: string
  /** 页面上出现的别的任务编号 → 标题和状态（编号可点、旁边标状态）。 */
  linked: Record<string, { title: string; status: WorkTaskStatus }>
  events: WorkTaskEvent[]
  /** 给 Agent 看的那一层：页面上默认折叠。 */
  agent: {
    brief: string; digest: string; report: string
    waiting_on: Record<string, unknown> | null; runs: WorkTaskRun[]
    decision: {
      id: number; probability: number | null; outcome: string
      recent_messages: { sender: string; sender_kind: string; text: string }[]
      dispatch: Record<string, unknown> | null; created_at: string
    } | null
  }
}
export interface WorkTaskQuery {
  view?: WorkTaskView; status?: string; q?: string; include_shadow?: boolean; before_id?: number; limit?: number
  /** 把自动记下的接口任务、求助也列出来（默认不列）。 */
  records?: boolean
  /** 只看还没结束的。 */
  unfinished?: boolean
}
export interface AssigneeIn { type: 'human' | 'bot'; id: string }
export interface WorkTaskCreate {
  title: string; summary?: string; brief?: string; assignee?: AssigneeIn | null
  reviewer_user_id?: string | null; review_required?: boolean | null
  priority?: string; due_at?: string | null
  /** 交给 AI 员工时建好就开工。 */
  start?: boolean
}
export interface WorkTaskPatch {
  title?: string; summary?: string; brief?: string; priority?: string; due_at?: string | null
  reviewer_user_id?: string | null; review_required?: boolean
}
export interface WorkTaskStats {
  days: number
  backlog: { assignee: WorkTaskActor | null; open: number; overdue: number }[]
  by_source: { source: string; record: boolean; created: number; done: number; avg_hours: number | null }[]
  handoffs_per_task: number | null; agent_first_pass_rate: number | null; agent_reviewed: number
}
const base = '/api/admin/work-tasks'
const one = (code: string) => `${base}/${encodeURIComponent(code)}`
export const workTasks = {
  list: (params: WorkTaskQuery = {}) =>
    call<{
      items: (WorkTask & { id: number })[]; counts: Record<string, number>; needs_me: number
      /** 「待我处理」才有：还在进行的事里，后续步骤点到我名字的。 */
      upcoming?: { code: string; title: string; step: string }[]
    }>(http.get(base, { params })),
  get: (code: string) => call<WorkTaskDetail>(http.get(one(code))),
  /** 给 AI 读的纯文本（Markdown）版。 */
  markdown: async (code: string): Promise<string> =>
    (await http.get<string>(one(code), { params: { format: 'md' }, responseType: 'text' })).data,
  stats: (days = 30) => call<WorkTaskStats>(http.get(`${base}/stats`, { params: { days } })),
  create: (body: WorkTaskCreate) => call<WorkTaskDetail>(http.post(base, body)),
  assign: (code: string, body: { assignee: AssigneeIn; note?: string; review_required?: boolean | null; start?: boolean }) =>
    call<WorkTaskDetail>(http.post(`${one(code)}/assign`, body)),
  /** 交付后还要人处理的事：勾成办了、改回没办，或者建成一件新任务。 */
  followup: (code: string, index: number, body: { done?: boolean; as_task?: boolean }) =>
    call<WorkTaskDetail>(http.post(`${one(code)}/followups/${index}`, body)),
  /** 叫任务上的 AI 员工开工或接着做：以点的人的身份做。 */
  start: (code: string) => call<WorkTaskDetail>(http.post(`${one(code)}/start`)),
  answer: (code: string, body: { ask_id: string; option: string; text?: string }) =>
    call<WorkTaskDetail>(http.post(`${one(code)}/answer`, body)),
  comment: (code: string, text: string) => call<WorkTaskDetail>(http.post(`${one(code)}/comment`, { text })),
  complete: (code: string, text: string) => call<WorkTaskDetail>(http.post(`${one(code)}/complete`, { text })),
  cancel: (code: string, text: string) => call<WorkTaskDetail>(http.post(`${one(code)}/cancel`, { text })),
  reopen: (code: string, text: string) => call<WorkTaskDetail>(http.post(`${one(code)}/reopen`, { text })),
  update: (code: string, body: WorkTaskPatch) => call<WorkTaskDetail>(http.patch(one(code), body)),
  watch: (code: string, on: boolean) => call<WorkTaskDetail>(http.post(`${one(code)}/watch`, { on })),
}
