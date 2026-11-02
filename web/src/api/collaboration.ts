import { call, http } from './client'
export interface BotHelpSettings { accept_bot_help: boolean; permission: 'granted' | 'missing' | 'pending' | 'unknown'; active_count: number }
const base = (id: string) => `/api/admin/bots/${encodeURIComponent(id)}`
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
  get: (id: string, signal?: AbortSignal) => call<BotHelpSettings>(http.get(`${base(id)}/bot-help`, { signal })),
  set: (id: string, enabled: boolean) => call<BotHelpSettings>(http.patch(`${base(id)}/bot-help`, { enabled })),
}
