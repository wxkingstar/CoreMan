import { describe, expect, it } from 'vitest'
import { formatDeliveryError } from '@/components/cron/deliveryError'
import { i18n } from '@/i18n'

describe('cron delivery errors', () => {
  it('explains known recipient failures with actionable guidance and keeps unknown details', () => {
    const t = i18n.global.t
    expect(formatDeliveryError('no_private_chat_or_unambiguous_notification_app', t)).toContain('当前机器人')
    expect(formatDeliveryError('recipient_disabled', t)).toContain('停用')
    expect(formatDeliveryError('recipient_unbound', t)).toContain('绑定')
    expect(formatDeliveryError('Feishu 99991672', t)).toBe('Feishu 99991672')
    expect(formatDeliveryError(null, t)).toBe('—')
  })
})
