import { call, http } from './client'

export interface ObserveMessage { sender: string; sender_kind: 'human' | 'bot' | 'self'; text: string }
export interface ObserveDecision {
  id: number; chat_id: string; mode: 'shadow' | 'on'; outcome: string; would_speak: boolean
  probability: number | null; model: string | null; latency_ms: number | null; input_tokens: number | null
  messages: ObserveMessage[]; task_id: number | null; review: 'should' | 'should_not' | null; created_at: string
}
export interface ObserveChat { chat_id: string; total: number; would_speak: number; last_at: string }
export interface ObserveQuery { chat_id?: string; would_speak?: boolean; before_id?: number; limit?: number }
const base = (id: string) => `/api/admin/bots/${id}/group-observe`
export const groupObserve = {
  decisions: (id: string, params: ObserveQuery = {}) =>
    call<{ items: ObserveDecision[]; chats: ObserveChat[] }>(http.get(`${base(id)}/decisions`, { params })),
  review: (id: string, decision: number, review: ObserveDecision['review']) =>
    call<ObserveDecision>(http.patch(`${base(id)}/decisions/${decision}`, { review })),
}
