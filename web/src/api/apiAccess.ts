import { call, http } from './client'

/** 令牌可驱动的 AI 员工；`api_access` 为 false 时它还没开「允许 API 驱动」，调用会被拒。 */
export interface ApiTokenBot { id: string; name: string; bot_key: string; api_access: boolean }

/** 本人的一枚 API 令牌。明文只在创建的响应里（`token`）出现一次。 */
export interface ApiToken {
  id: string; name: string; prefix: string; bots: ApiTokenBot[]
  status: 'active' | 'expired' | 'revoked'
  expires_at: string; last_used_at: string | null; revoked_at: string | null; created_at: string
  token?: string
}

export interface ApiAlias { id: string; alias: string; chat_id: string; created_by: string | null; created_at: string }
export interface BotApiAccess { enabled: boolean; aliases: ApiAlias[] }

const me = '/api/me'
const bot = (id: string) => `/api/admin/bots/${encodeURIComponent(id)}`

export const apiTokens = {
  list: () => call<ApiToken[]>(http.get(`${me}/api-tokens`)),
  bots: () => call<ApiTokenBot[]>(http.get(`${me}/api-token-bots`)),
  create: (name: string, botIds: string[], days: number) =>
    call<ApiToken>(http.post(`${me}/api-tokens`, { name, bot_ids: botIds, days })),
  revoke: (id: string) => call<ApiToken>(http.delete(`${me}/api-tokens/${encodeURIComponent(id)}`)),
}

export const botApiAccess = {
  get: (id: string, signal?: AbortSignal) => call<BotApiAccess>(http.get(`${bot(id)}/api-access`, { signal })),
  set: (id: string, enabled: boolean) => call<BotApiAccess>(http.patch(`${bot(id)}/api-access`, { enabled })),
  removeAlias: (id: string, aliasId: string) =>
    call<BotApiAccess>(http.delete(`${bot(id)}/api-aliases/${encodeURIComponent(aliasId)}`)),
}
