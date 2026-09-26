import { call, http } from './client'
export interface CollaborationRoute {
  id: string; target_bot_id: string; target_name: string; target_description: string | null
  enabled: boolean; version: number; status: 'ready' | 'verified' | 'runtime_ready' | 'unavailable'
  reason: string | null; configured?: boolean; runtime_ready?: boolean; transport_verified?: boolean
  verification_error?: string | null; can_enable: boolean; can_remove: boolean; active_count: number
}
export interface CollaborationPeer { id: string; name: string; description: string | null; enabled: boolean; available: boolean }
const base = (id: string) => `/api/admin/bots/${encodeURIComponent(id)}`
const route = (id: string, row: string) => `${base(id)}/collaborators/${encodeURIComponent(row)}`
const version = (v: number) => ({ headers: { 'If-Match': `"${v}"` } })
export interface HumanCollaborator {
  id: string; user_id: string; name: string; position: string; department: string; responsibility: string
  enabled: boolean; version: number; reachable: boolean; active_count: number
}
export interface HumanOption { id: string; name: string; login_name: string | null; position: string; department: string; added: boolean }
const human = (id: string, row?: string) => `${base(id)}/human-collaborators${row ? `/${encodeURIComponent(row)}` : ''}`
export const humanCollaboration = {
  list: (id: string, signal?: AbortSignal) => call<HumanCollaborator[]>(http.get(human(id), { signal })),
  options: (id: string, q: string, signal?: AbortSignal) => call<HumanOption[]>(http.get(`${base(id)}/human-collaborator-options`, { params: { q }, signal })),
  create: (id: string, userId: string, responsibility: string) => call<HumanCollaborator>(http.post(human(id), { user_id: userId, responsibility })),
  update: (id: string, row: string, body: { enabled?: boolean; responsibility?: string }, v: number) => call<HumanCollaborator>(http.patch(human(id, row), body, version(v))),
  remove: (id: string, row: string, v: number) => call<void>(http.delete(human(id, row), version(v)).then(res => ({ ...res, data: { code: 0, data: undefined } }))),
}
export const collaboration = {
  list: (id: string, signal?: AbortSignal) => call<CollaborationRoute[]>(http.get(`${base(id)}/collaborators`, { signal })),
  options: (id: string, q: string, signal?: AbortSignal) => call<CollaborationPeer[]>(http.get(`${base(id)}/collaborator-options`, { params: { q }, signal })),
  create: (id: string, target: string) => call<CollaborationRoute>(http.post(`${base(id)}/collaborators`, { target_bot_id: target })),
  update: (id: string, row: string, enabled: boolean, v: number) => call<CollaborationRoute>(http.patch(route(id, row), { enabled }, version(v))),
  remove: (id: string, row: string, v: number) => call<void>(http.delete(route(id, row), version(v)).then(res => ({ ...res, data: { code: 0, data: undefined } }))),
}
