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
}
export interface WorkTaskEvent {
  id: number; kind: string; actor_type: 'human' | 'bot' | 'system'; actor_name: string
  human_text: string; agent_detail: Record<string, unknown>; created_at: string
}
export interface WorkTaskRun {
  id: number; bot: WorkTaskBot | null; trigger: string; triggered_by: Record<string, unknown>
  status: string; outcome: string; session_key: string; chat_task_id: number
  started_at: string; ended_at: string | null
}
export interface WorkTaskDetail extends WorkTask {
  watching: boolean; watchers: { id: string; name: string }[]
  parent: { code: string; title: string } | null; children: WorkTask[]
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
}
export interface AssigneeIn { type: 'human' | 'bot'; id: string }
export interface WorkTaskCreate {
  title: string; summary?: string; brief?: string; assignee?: AssigneeIn | null
  reviewer_user_id?: string | null; review_required?: boolean | null
  priority?: string; due_at?: string | null
}
export interface WorkTaskPatch {
  title?: string; summary?: string; brief?: string; priority?: string; due_at?: string | null
  reviewer_user_id?: string | null; review_required?: boolean
}
const base = '/api/admin/work-tasks'
const one = (code: string) => `${base}/${encodeURIComponent(code)}`
export const workTasks = {
  list: (params: WorkTaskQuery = {}) =>
    call<{ items: (WorkTask & { id: number })[]; counts: Record<string, number>; needs_me: number }>(
      http.get(base, { params }),
    ),
  get: (code: string) => call<WorkTaskDetail>(http.get(one(code))),
  create: (body: WorkTaskCreate) => call<WorkTaskDetail>(http.post(base, body)),
  assign: (code: string, body: { assignee: AssigneeIn; note?: string; review_required?: boolean | null }) =>
    call<WorkTaskDetail>(http.post(`${one(code)}/assign`, body)),
  answer: (code: string, body: { ask_id: string; option: string; text?: string }) =>
    call<WorkTaskDetail>(http.post(`${one(code)}/answer`, body)),
  comment: (code: string, text: string) => call<WorkTaskDetail>(http.post(`${one(code)}/comment`, { text })),
  complete: (code: string, text: string) => call<WorkTaskDetail>(http.post(`${one(code)}/complete`, { text })),
  cancel: (code: string, text: string) => call<WorkTaskDetail>(http.post(`${one(code)}/cancel`, { text })),
  reopen: (code: string, text: string) => call<WorkTaskDetail>(http.post(`${one(code)}/reopen`, { text })),
  update: (code: string, body: WorkTaskPatch) => call<WorkTaskDetail>(http.patch(one(code), body)),
  watch: (code: string, on: boolean) => call<WorkTaskDetail>(http.post(`${one(code)}/watch`, { on })),
}
