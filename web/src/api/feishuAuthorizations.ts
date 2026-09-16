import { call, http } from './client'
export interface FeishuAuthorization {
  bot_id: string
  bot_name: string
  status: 'connected' | 'pending' | 'expired' | 'revoked'
  scopes: string[]
  expires_at: string | null
}
const base = '/api/me/feishu-authorizations'
export const feishuAuthorizations = {
  list: () => call<{ items: FeishuAuthorization[] }>(http.get(base)),
  revoke: (botId: string) => call<{ ok: true }>(http.delete(`${base}/${encodeURIComponent(botId)}`)),
}
