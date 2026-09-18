import { call, http } from './client'

export type WecomProvisionStatus = 'pending' | 'succeeded' | 'consumed' | 'expired' | 'failed' | 'cancelled'
export interface WecomProvision {
  id: string
  status: WecomProvisionStatus
  bot_id: string | null
  /** 企业微信侧的机器人 Bot ID；Secret 只在服务端流转。 */
  wecom_bot_id: string | null
  /** 二维码内容，只在二维码有效期内返回给发起人本人。 */
  url: string | null
  /** 企业微信返回的最近一次状态（init / pending …）。 */
  upstream_status: string | null
  /** 凭证是否已通过长连接订阅校验。 */
  verified: boolean
  expires_at: string
  retry_after: number | null
  error: string | null
  created_at: string | null
  reused?: boolean
}

const provisions = '/api/admin/wecom-bot-provisions'
export const wecomBots = {
  startProvision: (body: { reuse: boolean }) => call<WecomProvision>(http.post(provisions, body)),
  provision: (id: string) => call<WecomProvision>(http.get(`${provisions}/${encodeURIComponent(id)}`)),
  cancelProvision: (id: string) => call<WecomProvision>(http.delete(`${provisions}/${encodeURIComponent(id)}`)),
}
