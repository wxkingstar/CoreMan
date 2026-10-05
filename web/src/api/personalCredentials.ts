import { call, http } from '@/api/client'

export interface CredentialField { key: string; label: string; secret: boolean; placeholder: string }

/** AI 员工发起的一次索取。不含任何值。`save` 为 false 时是一次性交付：不保存，用完即删。 */
export interface CredentialRequest {
  id: string; bot_name: string; platform: string; purpose: string; fields: CredentialField[]; save: boolean
  status: 'open' | 'submitted' | 'expired' | 'cancelled'; expires_at: string; security_note: string
}

/** 本人保存的一条凭证：密文字段的 value 恒为 null。 */
export interface PersonalCredential {
  bot_id: string; bot_name: string; env_key: string; label: string; secret: boolean
  value: string | null; updated_at: string; last_used_at: string | null
}

const requests = '/api/me/credential-requests'
const root = '/api/me/credentials'
export const personalCredentials = {
  request: (id: string) => call<CredentialRequest>(http.get(`${requests}/${id}`)),
  submit: (id: string, values: Record<string, string>) =>
    call<{ status: string; keys: string[]; message: string }>(http.post(`${requests}/${id}/submit`, { values })),
  list: () => call<PersonalCredential[]>(http.get(root)),
  update: (botId: string, key: string, value: string) =>
    call<PersonalCredential>(http.put(`${root}/${botId}/${key}`, { value })),
  remove: (botId: string, key: string) => call<null>(http.delete(`${root}/${botId}/${key}`)),
}
