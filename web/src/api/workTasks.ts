import { call, http } from './client'

export type WorkTaskStatus =
  | 'shadow' | 'pending' | 'queued' | 'running' | 'awaiting_confirmation'
  | 'done' | 'unfinished' | 'cancelled' | 'needs_human'
export interface WorkTaskBot { id: string; name: string }
/** 给人看的那一层。 */
export interface WorkTask {
  code: string; title: string; summary: string; status: WorkTaskStatus
  platform: string; chat_id: string; requester_name: string
  assignee: WorkTaskBot | null; dispatcher: WorkTaskBot | null; related_codes: string[]
  created_at: string; updated_at: string; closed_at: string | null
}
export interface WorkTaskEvent {
  id: number; kind: string; actor_type: 'human' | 'bot' | 'system'; actor_name: string
  human_text: string; agent_detail: Record<string, unknown>; created_at: string
}
export interface WorkTaskDetail extends WorkTask {
  events: WorkTaskEvent[]
  /** 给 Agent 看的那一层：页面上逐项展开。 */
  agent: {
    brief: string; digest: string
    decision: {
      id: number; probability: number | null; outcome: string
      recent_messages: { sender: string; sender_kind: string; text: string }[]
      dispatch: Record<string, unknown> | null; created_at: string
    } | null
  }
}
export interface WorkTaskQuery { status?: string; q?: string; before_id?: number; limit?: number }
const base = '/api/admin/work-tasks'
export const workTasks = {
  list: (params: WorkTaskQuery = {}) =>
    call<{ items: (WorkTask & { id: number })[]; counts: Record<string, number> }>(http.get(base, { params })),
  get: (code: string) => call<WorkTaskDetail>(http.get(`${base}/${encodeURIComponent(code)}`)),
}
