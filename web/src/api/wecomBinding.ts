import { call, http } from './client'

export type WecomAuthorizationLevel = 'readonly' | 'all_except_send' | 'all'
export type CapabilityState = 'ok' | 'unauthorized' | 'expired' | 'invalid' | 'error' | 'unknown'
export interface CapabilityKind {
  state: CapabilityState
  checked_at: string | null
  /** 企业微信不提供到期时间：按上次授权加 7 天估算，只有 ok 时才有。 */
  expires_at: string | null
  /** 企业微信给的授权/续期页面，只能在电脑端打开。 */
  renew_url: string | null
}
export type CapabilityService = 'contact' | 'todo' | 'calendar' | 'meeting' | 'doc' | 'mail' | 'disk'
export interface Capability {
  service: CapabilityService
  /** null 表示还没观察到这一项。 */
  read: CapabilityKind | null
  write: CapabilityKind | null
  send: CapabilityKind | null
}
export type ScanStatus = 'pending' | 'succeeded' | 'expired' | 'failed' | 'cancelled'
export interface ScanSession {
  status: ScanStatus
  /** 二维码内容，等同于授权机器人的密钥：只在有效期内返回给本人。 */
  url: string | null
  upstream_status: string | null
  expires_at: string | null
  error: string | null
  retry_after: number | null
}
export interface WecomBinding {
  status: 'unbound' | 'bound'
  /** false 表示本人在私聊或本页暂停了使用，凭证仍保留。 */
  enabled: boolean
  authorization_level: WecomAuthorizationLevel
  wecom_bot_id: string | null
  bot_name: string | null
  authorizer_name: string | null
  bound_at: string | null
  verified_at: string | null
  next_expiry: string | null
  error: string | null
  capabilities: Capability[]
  retention_notice: string
  scan: ScanSession | null
  identity_linked: boolean
  auth_ttl_days: number
}

const base = '/api/me/wecom-binding'
const scan = `${base}/scan`
export const wecomBinding = {
  get: () => call<WecomBinding>(http.get(base)),
  startScan: () => call<WecomBinding>(http.post(scan)),
  pollScan: () => call<WecomBinding>(http.get(scan)),
  cancelScan: () => call<WecomBinding>(http.delete(scan)),
  update: (body: { authorization_level?: WecomAuthorizationLevel; enabled?: boolean }) =>
    call<WecomBinding>(http.patch(base, body)),
  check: () => call<WecomBinding>(http.post(`${base}/check`)),
  renewed: () => call<WecomBinding>(http.post(`${base}/renewed`)),
  unbind: () => call<WecomBinding>(http.delete(base)),
}
