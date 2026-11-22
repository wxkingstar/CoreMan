import { call, http } from '@/api/client'

export interface CredentialField { key: string; label: string; secret: boolean; placeholder: string }

/** 表单上可以直接选用的一条已有凭证：只有名字和在用它的 AI 员工，不含值。 */
export interface CredentialChoice { id: string; label: string; env_key: string; bots: string[] }

/** AI 员工发起的一次索取。不含任何值。`save` 为 false 时是一次性交付：不保存，用完即删。 */
export interface CredentialRequest {
  id: string; bot_name: string; platform: string; purpose: string; fields: CredentialField[]; save: boolean
  status: 'open' | 'submitted' | 'expired' | 'cancelled'; expires_at: string; security_note: string
  /** 按变量名列出可选的已有凭证；一次性交付与已关闭的表单为空。 */
  choices?: Record<string, CredentialChoice[]>
  /** 默认选中的已有凭证（只有一条同名候选时）。 */
  defaults?: Record<string, string>
}

export interface CredentialBinding { bot_id: string; bot_name: string; env_key: string; last_used_at: string | null }

/** 本人的一条凭证：一份值，可以绑给多个 AI 员工。密文字段的 value 恒为 null。 */
export interface CredentialItem {
  id: string; env_key: string; label: string; secret: boolean; value: string | null
  updated_at: string; last_used_at: string | null; bindings: CredentialBinding[]
}

export interface CredentialBot { id: string; name: string; platform: string }

const requests = '/api/me/credential-requests'
const items = '/api/me/credential-items'
export const personalCredentials = {
  request: (id: string) => call<CredentialRequest>(http.get(`${requests}/${id}`)),
  submit: (id: string, values: Record<string, string>, use: Record<string, string> = {}) =>
    call<{ status: string; keys: string[]; message: string }>(http.post(`${requests}/${id}/submit`, { values, use })),
  items: () => call<CredentialItem[]>(http.get(items)),
  bots: () => call<CredentialBot[]>(http.get('/api/me/credential-bots')),
  updateItem: (id: string, value: string) => call<CredentialItem>(http.put(`${items}/${id}`, { value })),
  rename: (id: string, label: string) => call<CredentialItem>(http.put(`${items}/${id}/label`, { label })),
  setBots: (id: string, botIds: string[]) => call<CredentialItem>(http.put(`${items}/${id}/bots`, { bot_ids: botIds })),
  removeItem: (id: string) => call<null>(http.delete(`${items}/${id}`)),
}
