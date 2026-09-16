import { call, http } from './client'
export interface FeishuAuthorization {
  bot_id: string
  bot_name: string
  status: 'connected' | 'selecting' | 'pending' | 'expired' | 'revoked'
  authorization_level?: 'legacy_readonly' | 'messages_readonly' | 'all_except_send' | 'all'
  requested_scopes?: string[]
  missing_scopes?: string[]
  scopes: string[]
  expires_at: string | null
}
const base = '/api/me/feishu-authorizations'
export const feishuAuthorizations = {
  list: () => call<{ items: FeishuAuthorization[] }>(http.get(base)),
  revoke: (botId: string) => call<{ ok: true; remote_revoked: boolean }>(http.delete(`${base}/${encodeURIComponent(botId)}`)),
}
