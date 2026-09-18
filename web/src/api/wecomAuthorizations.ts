import { call, http } from './client'
export interface WecomAuthorization {
  bot_id: string
  bot_name: string
  status: 'selecting' | 'connected' | 'revoked' | 'expired'
  authorization_level: 'readonly' | 'all_except_send' | 'all'
  verified_at: string | null
  selection_expires_at: string | null
  retention_notice?: string
}
const base = '/api/me/wecom-authorizations'
export const wecomAuthorizations = {
  list: () => call<{ items: WecomAuthorization[] }>(http.get(base)),
  revoke: (botId: string) => call<{ ok: true }>(http.delete(`${base}/${encodeURIComponent(botId)}`)),
}
