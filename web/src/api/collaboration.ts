import { call, http } from './client'
export interface CollaborationRoute {
  id: string; target_bot_id: string; target_name: string; target_description: string | null
  chat_id: string; chat_name: string; enabled: boolean; version: number
  status: 'pending' | 'ready' | 'failed' | 'expired' | 'unavailable'
  reason: string | null; can_enable: boolean; can_verify: boolean; can_remove: boolean; active_count: number
}
export interface CollaborationPeer { id: string; name: string; description: string | null; enabled: boolean; available: boolean }
export interface CollaborationGroup { chat_id: string; name: string }
// Platform discovery/verification has a 30-second server deadline.
const platformTimeout = 35000
const base = (id: string) => `/api/admin/bots/${encodeURIComponent(id)}`
const route = (id: string, row: string) => `${base(id)}/collaborators/${encodeURIComponent(row)}`
const version = (v: number) => ({ headers: { 'If-Match': `"${v}"` } })
export const collaboration = {
  list: (id: string, signal?: AbortSignal) => call<CollaborationRoute[]>(http.get(`${base(id)}/collaborators`, { signal })),
  options: (id: string, q: string, signal?: AbortSignal) => call<CollaborationPeer[]>(http.get(`${base(id)}/collaborator-options`, { params: { q }, signal, timeout: platformTimeout })),
  groups: (id: string, target: string, signal?: AbortSignal) => call<CollaborationGroup[]>(http.get(`${base(id)}/collaborator-groups`, { params: { target_bot_id: target }, signal, timeout: platformTimeout })),
  create: (id: string, target: string, chat: string) => call<CollaborationRoute>(http.post(`${base(id)}/collaborators`, { target_bot_id: target, chat_id: chat }, { timeout: platformTimeout })),
  verify: (id: string, row: string, v: number) => call<CollaborationRoute>(http.post(`${route(id, row)}/verify`, {}, { ...version(v), timeout: platformTimeout })),
  update: (id: string, row: string, enabled: boolean, v: number) => call<CollaborationRoute>(http.patch(route(id, row), { enabled }, version(v))),
  remove: (id: string, row: string, v: number) => call<void>(http.delete(route(id, row), version(v)).then(res => ({ ...res, data: { code: 0, data: undefined } }))),
}
