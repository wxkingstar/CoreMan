import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,qr') } }))
vi.mock('@/api/wecomBinding', () => ({
  wecomBinding: { get: vi.fn(), startScan: vi.fn(), pollScan: vi.fn(), cancelScan: vi.fn(), update: vi.fn(), check: vi.fn(), renewed: vi.fn(), unbind: vi.fn() },
}))

import QRCode from 'qrcode'
import { wecomBinding, type Capability, type CapabilityKind, type ScanSession, type WecomBinding } from '@/api/wecomBinding'
import { i18n } from '@/i18n'
import { formatDateTime } from '@/utils/format'
import MyWecomView from '@/views/MyWecomView.vue'

const SERVICES = ['contact', 'todo', 'calendar', 'meeting', 'doc', 'mail', 'disk'] as const
const QR_URL = 'https://work.weixin.qq.com/ai/qc/c?s=Ab12Cd34Ef56Gh78&hide_more_btn=true'
const RENEW_URL = 'https://work.weixin.qq.com/help?person_id=example'
const tr = (key: string, params: Record<string, unknown> = {}) => i18n.global.t(key, params)
const api = vi.mocked(wecomBinding)

function kind(state: CapabilityKind['state'], over: Partial<CapabilityKind> = {}): CapabilityKind {
  return { state, checked_at: '2026-09-19T02:00:00Z', expires_at: state === 'ok' ? '2026-09-26T02:00:00Z' : null, renew_url: null, ...over }
}
function caps(read: (service: string) => CapabilityKind | null = () => kind('ok')): Capability[] {
  return SERVICES.map((service) => ({ service, read: read(service), write: null, send: null }))
}
function unbound(over: Partial<WecomBinding> = {}): WecomBinding {
  return {
    status: 'unbound', enabled: false, authorization_level: 'readonly', wecom_bot_id: null, bot_name: null, authorizer_name: null,
    bound_at: null, verified_at: null, next_expiry: null, error: null, capabilities: [], retention_notice: 'server retention notice',
    scan: null, identity_linked: true, auth_ttl_days: 7, ...over,
  }
}
function bound(over: Partial<WecomBinding> = {}): WecomBinding {
  return unbound({
    status: 'bound', enabled: true, authorization_level: 'all_except_send', wecom_bot_id: 'aib-1', bot_name: '示例助理',
    authorizer_name: '示例成员', bound_at: '2026-09-10T02:00:00Z', verified_at: '2026-09-19T01:00:00Z',
    next_expiry: '2026-09-26T02:00:00Z', capabilities: caps(), ...over,
  })
}
function scan(over: Partial<ScanSession> = {}): ScanSession {
  return { status: 'pending', url: QR_URL, upstream_status: 'init', expires_at: '2026-09-19T08:05:00Z', error: null, retry_after: 3, ...over }
}
const render = () => mount(MyWecomView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
const body = () => document.body.textContent ?? ''
const el = (selector: string) => document.querySelector(selector)
const click = async (selector: string) => { (el(selector) as HTMLElement).click(); await flushPromises() }

beforeEach(() => {
  vi.restoreAllMocks()
  for (const fn of Object.values(api)) vi.mocked(fn).mockReset()
  api.get.mockResolvedValue(bound())
  vi.mocked(QRCode.toDataURL).mockClear()
})
afterEach(() => { vi.useRealTimers(); document.body.innerHTML = '' })

describe('guide', () => {
  it('explains the per-member bot, chat commands, tiers, the 7-day renewal and the server privacy notice', async () => {
    const w = render(); await flushPromises()
    const guide = w.get('.wecom-connect-guide').text()
    expect(guide).toContain('连接企业微信')
    expect(guide).toContain('断开企业微信')
    for (const key of ['model', 'scope', 'renewDone']) expect(guide).toContain(tr(`myWecom.guide.${key}`))
    for (const level of ['readonly', 'all_except_send', 'all']) {
      expect(guide).toContain(tr(`myWecom.levels.${level}`))
      expect(guide).toContain(tr(`myWecom.capabilityHints.${level}`))
    }
    // 续期只能在电脑端，路径里带上本人授权机器人的名字。
    expect(guide).toContain(tr('myWecom.guide.renew', { days: 7, path: tr('myWecom.renewPath', { bot: '示例助理' }) }))
    expect(w.get('[data-test="retention-notice"]').text()).toBe('server retention notice')
    w.unmount()
  })
})

describe('unbound', () => {
  it('refuses to scan until the WeCom account is synced and says to contact an admin', async () => {
    api.get.mockResolvedValue(unbound({ identity_linked: false }))
    const w = render(); await flushPromises()
    expect(w.get('[data-test="unbound"]').text()).toContain(tr('myWecom.unbound.title'))
    expect(w.get('[data-test="scan"]').attributes('disabled')).toBeDefined()
    expect(w.get('[data-test="identity-unlinked"]').text()).toBe(tr('myWecom.unbound.identityUnlinked'))
    await w.get('[data-test="scan"]').trigger('click'); await flushPromises()
    expect(api.startScan).not.toHaveBeenCalled()
    w.unmount()
  })

  it('explains that the previous bot was deleted or reset and must be rebound', async () => {
    api.get.mockResolvedValue(unbound({ error: 'credentials_rejected', bot_name: '旧助理' }))
    const w = render(); await flushPromises()
    expect(w.get('[data-test="binding-error"]').text()).toContain(tr('myWecom.errors.credentials_rejected'))
    expect(w.get('[data-test="scan"]').attributes('disabled')).toBeUndefined()
    expect(w.find('[data-test="identity-unlinked"]').exists()).toBe(false)
    w.unmount()
  })

  it('offers retry after a load failure without showing raw errors', async () => {
    api.get.mockRejectedValueOnce(new Error('secret-token'))
    const w = render(); await flushPromises()
    expect(w.text()).toContain(tr('myWecom.loadError'))
    expect(w.text()).not.toContain('secret-token')
    await w.findAll('button').find((b) => b.text() === tr('workspace.retry'))!.trigger('click'); await flushPromises()
    expect(w.find('[data-test="binding"]').exists()).toBe(true)
    w.unmount()
  })
})

describe('scan to bind', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }) })

  it('draws the QR code with the secret warning and steps, and polls until the member is bound', async () => {
    api.get.mockResolvedValue(unbound())
    api.startScan.mockResolvedValue(unbound({ scan: scan() }))
    api.pollScan
      .mockResolvedValueOnce(unbound({ scan: scan({ upstream_status: 'scanned' }) }))
      .mockResolvedValueOnce(bound({ scan: scan({ status: 'succeeded', url: null, upstream_status: null }) }))
    const success = vi.spyOn(ElMessage, 'success')
    const w = render(); await flushPromises()
    await w.get('[data-test="scan"]').trigger('click'); await flushPromises()
    expect(api.startScan).toHaveBeenCalledTimes(1)
    expect(QRCode.toDataURL).toHaveBeenCalledWith(QR_URL, expect.anything())
    expect(el('[data-test="bind-qr"]')).not.toBeNull()
    expect(el('[data-test="bind-secret-warning"]')?.textContent).toContain(tr('myWecom.scan.secret'))
    const steps = el('[data-test="bind-steps"]')?.textContent ?? ''
    for (const step of ['step1', 'step2', 'step3']) expect(steps).toContain(tr(`myWecom.scan.${step}`))
    expect(steps).toContain('确认授权')
    // QR 内容只出现在「在企业微信中打开」这一个链接里，不以文字形式展示。
    const open = el('[data-test="bind-open-in-wecom"]') as HTMLAnchorElement
    expect(open.getAttribute('href')).toBe(QR_URL)
    expect(open.target).toBe('_blank')
    expect(open.rel).toBe('noopener noreferrer')
    expect(open.textContent).toBe(tr('myWecom.scan.openInWecom'))
    expect(document.querySelectorAll(`a[href="${QR_URL}"]`)).toHaveLength(1)
    expect(body()).not.toContain(QR_URL)

    await vi.advanceTimersByTimeAsync(3000); await flushPromises()
    expect(api.pollScan).toHaveBeenCalledTimes(1)
    expect(el('[data-test="bind-progress"]')?.textContent).toContain(tr('myWecom.scan.scanned'))
    await vi.advanceTimersByTimeAsync(3000); await flushPromises()
    expect(api.pollScan).toHaveBeenCalledTimes(2)
    expect(success).toHaveBeenCalledWith(tr('myWecom.boundSuccess'))
    expect(w.findComponent({ name: 'WecomBindDialog' }).props('visible')).toBe(false)
    expect(w.get('[data-test="bot-name"]').text()).toBe('示例助理')
    expect(api.cancelScan).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.pollScan).toHaveBeenCalledTimes(2)
    w.unmount()
  })

  it('keeps polling through a network error and says so', async () => {
    api.get.mockResolvedValue(unbound())
    api.startScan.mockResolvedValue(unbound({ scan: scan() }))
    api.pollScan.mockRejectedValueOnce(new Error('Network Error')).mockResolvedValue(unbound({ scan: scan() }))
    const w = render(); await flushPromises()
    await w.get('[data-test="scan"]').trigger('click'); await flushPromises()
    await vi.advanceTimersByTimeAsync(3000); await flushPromises()
    expect(el('[data-test="bind-poll-error"]')?.textContent).toContain('Network Error')
    await vi.advanceTimersByTimeAsync(5000); await flushPromises()
    expect(api.pollScan).toHaveBeenCalledTimes(2)
    expect(el('[data-test="bind-poll-error"]')).toBeNull()
    w.unmount()
  })

  it('maps failure reasons and lets the member generate a new QR code', async () => {
    api.get.mockResolvedValue(unbound())
    api.startScan
      .mockResolvedValueOnce(unbound({ scan: scan({ status: 'failed', url: null, error: 'not_self' }) }))
      .mockResolvedValueOnce(unbound({ scan: scan({ status: 'expired', url: null, error: 'qr_expired' }) }))
      .mockResolvedValueOnce(unbound({ scan: scan({ status: 'failed', url: null, error: 'brand_new_reason' }) }))
      .mockResolvedValueOnce(unbound({ scan: scan() }))
    const w = render(); await flushPromises()
    await w.get('[data-test="scan"]').trigger('click'); await flushPromises()
    let failed = el('[data-test="bind-failed"]')?.textContent ?? ''
    expect(failed).toContain(tr('myWecom.scan.status.failed'))
    expect(failed).toContain(tr('myWecom.scan.error.not_self'))
    await click('[data-test="bind-retry"]')
    failed = el('[data-test="bind-failed"]')?.textContent ?? ''
    expect(failed).toContain(tr('myWecom.scan.status.expired'))
    expect(failed).toContain(tr('myWecom.scan.error.qr_expired'))
    await click('[data-test="bind-retry"]')
    expect(el('[data-test="bind-failed"]')?.textContent).toContain(tr('myWecom.scan.error.unknown'))
    await click('[data-test="bind-retry"]')
    expect(api.startScan).toHaveBeenCalledTimes(4)
    expect(el('[data-test="bind-qr"]')).not.toBeNull()
    w.unmount()
  })

  it('shows the server reason when the scan cannot start', async () => {
    api.get.mockResolvedValue(unbound())
    api.startScan.mockRejectedValueOnce(new Error('你的企业微信账号还没有同步'))
    const w = render(); await flushPromises()
    await w.get('[data-test="scan"]').trigger('click'); await flushPromises()
    expect(el('[data-test="bind-failed"]')?.textContent).toContain('你的企业微信账号还没有同步')
    expect(el('[data-test="bind-retry"]')).not.toBeNull()
    w.unmount()
  })

  it('cancels a pending scan when the dialog is closed and stops polling', async () => {
    api.get.mockResolvedValue(unbound())
    api.startScan.mockResolvedValue(unbound({ scan: scan() }))
    api.cancelScan.mockResolvedValue(unbound({ scan: scan({ status: 'cancelled', url: null, error: 'cancelled' }) }))
    const w = render(); await flushPromises()
    await w.get('[data-test="scan"]').trigger('click'); await flushPromises()
    w.findComponent({ name: 'ElDialog' }).vm.$emit('close'); await flushPromises()
    expect(api.cancelScan).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.pollScan).not.toHaveBeenCalled()
    w.unmount()
  })
})

describe('bound', () => {
  it('shows the bot, whom it represents, per-capability states and the estimated expiry', async () => {
    const states: Record<string, CapabilityKind | null> = {
      contact: kind('ok'), todo: kind('unauthorized', { renew_url: RENEW_URL }), calendar: kind('expired'), meeting: kind('invalid'),
      doc: kind('error'), mail: kind('ok'), disk: null,
    }
    api.get.mockResolvedValue(bound({ capabilities: caps((service) => states[service]) }))
    const w = render(); await flushPromises()
    expect(w.get('[data-test="bot-name"]').text()).toBe('示例助理')
    expect(w.get('[data-test="authorizer"]').text()).toBe('示例成员')
    expect(w.get('[data-test="binding-status"]').text()).toBe(tr('myWecom.status.enabled'))
    expect(w.text()).toContain(tr('myWecom.nextExpiry'))
    expect(w.get('[data-test="next-expiry"]').text()).toBe(formatDateTime('2026-09-26T02:00:00Z'))
    const tag = (service: string, k = 'read') => w.get(`[data-test="cap-${service}-${k}"] .el-tag`)
    const expected: [string, string, string][] = [
      ['contact', 'ok', 'success'], ['todo', 'unauthorized', 'warning'], ['calendar', 'expired', 'danger'],
      ['meeting', 'invalid', 'danger'], ['doc', 'unchecked', 'info'], ['disk', 'unchecked', 'info'],
    ]
    for (const [service, state, type] of expected) {
      expect(tag(service).text()).toBe(tr(`myWecom.states.${state}`))
      expect(tag(service).classes()).toContain(`el-tag--${type}`)
    }
    expect(tag('mail', 'send').text()).toBe(tr('myWecom.states.unchecked'))
    expect(w.get('[data-test="cap-contact"]').text()).toContain(tr('myWecom.services.contact'))
    expect(w.get('[data-test="cap-contact-read"]').text()).toContain(tr('myWecom.expiresShort', { time: formatDateTime('2026-09-26T02:00:00Z').slice(5, 16) }))
    expect(w.find('[data-test="cap-todo-read"] small').exists()).toBe(false)
    const renew = w.get('[data-test="renew-todo"]')
    expect(renew.attributes('href')).toBe(RENEW_URL)
    expect(renew.attributes('target')).toBe('_blank')
    expect(w.find('[data-test="renew-contact"]').exists()).toBe(false)
    expect(w.text()).toContain(tr('myWecom.renewLinkNote'))
    expect(w.get('[data-test="renew-needed"]').text()).toContain(tr('myWecom.renewPath', { bot: '示例助理' }))
    expect(w.find('[data-test="confirm-auth"]').exists()).toBe(false)
    w.unmount()
  })

  it('changes the tier and pauses through PATCH', async () => {
    const success = vi.spyOn(ElMessage, 'success')
    api.update
      .mockResolvedValueOnce(bound({ authorization_level: 'all' }))
      .mockResolvedValueOnce(bound({ authorization_level: 'all', enabled: false }))
    const w = render(); await flushPromises()
    await w.get('[data-test="level-all"] input').setValue(true); await flushPromises()
    expect(api.update).toHaveBeenLastCalledWith({ authorization_level: 'all' })
    expect(success).toHaveBeenLastCalledWith(tr('myWecom.saved'))
    expect((w.get('[data-test="level-all"] input').element as HTMLInputElement).checked).toBe(true)
    await w.get('[data-test="enabled-switch"]').trigger('click'); await flushPromises()
    expect(api.update).toHaveBeenLastCalledWith({ enabled: false })
    expect(success).toHaveBeenLastCalledWith(tr('myWecom.pausedDone'))
    expect(w.get('[data-test="binding-status"]').text()).toBe(tr('myWecom.status.paused'))
    expect(w.text()).toContain(tr('myWecom.pausedHint', { command: '连接企业微信' }))
    w.unmount()
  })

  it('re-checks, records a renewal after confirming, and shows server errors', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const success = vi.spyOn(ElMessage, 'success')
    const failure = vi.spyOn(ElMessage, 'error')
    api.check.mockResolvedValueOnce(bound()).mockRejectedValueOnce(new Error('暂时连不上企业微信'))
    api.renewed.mockResolvedValue(bound({ next_expiry: '2026-09-27T02:00:00Z' }))
    const w = render(); await flushPromises()
    await w.get('[data-test="check"]').trigger('click'); await flushPromises()
    expect(api.check).toHaveBeenCalledTimes(1)
    expect(success).toHaveBeenLastCalledWith(tr('myWecom.checked'))
    await w.get('[data-test="check"]').trigger('click'); await flushPromises()
    expect(failure).toHaveBeenCalledWith('暂时连不上企业微信')
    await w.get('[data-test="renewed"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalledWith(tr('myWecom.confirmRenewed', { days: 7 }), tr('myWecom.renewed'), { type: 'info' })
    expect(api.renewed).toHaveBeenCalledTimes(1)
    expect(success).toHaveBeenLastCalledWith(tr('myWecom.renewedDone'))
    expect(w.get('[data-test="next-expiry"]').text()).toBe(formatDateTime('2026-09-27T02:00:00Z'))
    w.unmount()
  })

  it('does not record a renewal when the confirmation is cancelled', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    const w = render(); await flushPromises()
    await w.get('[data-test="renewed"]').trigger('click'); await flushPromises()
    expect(api.renewed).not.toHaveBeenCalled()
    w.unmount()
  })

  it('unbinds only after confirming and reminds to delete the bot in WeCom', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel').mockResolvedValue('confirm' as never)
    const success = vi.spyOn(ElMessage, 'success')
    api.unbind.mockResolvedValue(unbound({ bot_name: '示例助理' }))
    const w = render(); await flushPromises()
    await w.get('[data-test="unbind"]').trigger('click'); await flushPromises()
    expect(api.unbind).not.toHaveBeenCalled()
    await w.get('[data-test="unbind"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenLastCalledWith(tr('myWecom.confirmUnbind', { bot: '示例助理' }), tr('myWecom.unbind'), { type: 'warning' })
    expect(api.unbind).toHaveBeenCalledTimes(1)
    expect(success).toHaveBeenCalledWith(tr('myWecom.unbindDone'))
    expect(w.find('[data-test="unbound"]').exists()).toBe(true)
    expect(w.find('[data-test="binding"]').exists()).toBe(false)
    w.unmount()
  })

  it('warns that scanning again creates a new bot before opening the QR code', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    api.startScan.mockResolvedValue(bound({ scan: scan() }))
    const w = render(); await flushPromises()
    await w.get('[data-test="rebind"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalledWith(tr('myWecom.confirmRebind', { bot: '示例助理' }), tr('myWecom.rebind'), { type: 'warning' })
    expect(api.startScan).toHaveBeenCalledTimes(1)
    expect(el('[data-test="bind-qr"]')).not.toBeNull()
    w.unmount()
  })

  it('does not let a stale background refresh restore the binding after unbinding', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    api.unbind.mockResolvedValue(unbound())
    const w = render(); await flushPromises()
    let resolve!: (value: WecomBinding) => void
    api.get.mockReturnValueOnce(new Promise((r) => { resolve = r }))
    window.dispatchEvent(new Event('focus')); await flushPromises()
    await w.get('[data-test="unbind"]').trigger('click'); await flushPromises()
    resolve(bound()); await flushPromises()
    expect(w.find('[data-test="unbound"]').exists()).toBe(true)
    w.unmount()
  })
})

describe('after binding', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    vi.setSystemTime(new Date('2026-09-19T08:00:00Z'))
  })
  const fresh = (read: CapabilityKind['state']) => bound({ bound_at: '2026-09-19T07:59:30Z', next_expiry: null, capabilities: caps(() => kind(read)) })

  it('asks to tap 确认授权 when nothing is authorized yet and re-checks until it works', async () => {
    let server = fresh('unauthorized')
    api.get.mockImplementation(async () => server)
    api.check.mockImplementationOnce(async () => server).mockImplementationOnce(async () => (server = fresh('ok')))
    const w = render(); await flushPromises()
    const alert = w.get('[data-test="confirm-auth"]')
    expect(alert.text()).toContain(tr('myWecom.confirmAuthTitle'))
    expect(alert.text()).toContain(tr('myWecom.confirmAuthHint', { path: tr('myWecom.renewPath', { bot: '示例助理' }) }))
    expect(w.find('[data-test="renew-needed"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.check).toHaveBeenCalledTimes(1)
    expect(w.find('[data-test="confirm-auth"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.check).toHaveBeenCalledTimes(2)
    expect(w.find('[data-test="confirm-auth"]').exists()).toBe(false)
    expect(w.get('[data-test="cap-contact-read"] .el-tag').text()).toBe(tr('myWecom.states.ok'))
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.check).toHaveBeenCalledTimes(2)
    w.unmount()
  })

  it('stops re-checking after two minutes and when leaving the page', async () => {
    api.get.mockResolvedValue(fresh('unauthorized'))
    api.check.mockResolvedValue(fresh('unauthorized'))
    const w = render(); await flushPromises()
    await vi.advanceTimersByTimeAsync(180000); await flushPromises()
    expect(api.check).toHaveBeenCalledTimes(12)
    w.unmount()

    api.check.mockClear()
    vi.setSystemTime(new Date('2026-09-19T08:00:00Z'))
    const again = render(); await flushPromises()
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.check).toHaveBeenCalledTimes(1)
    again.unmount()
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.check).toHaveBeenCalledTimes(1)
  })

  it('does not nag for bindings older than a few minutes', async () => {
    api.get.mockResolvedValue(bound({ bound_at: '2026-09-19T07:50:00Z', capabilities: caps(() => kind('unauthorized')) }))
    const w = render(); await flushPromises()
    expect(w.find('[data-test="confirm-auth"]').exists()).toBe(false)
    expect(w.find('[data-test="renew-needed"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(30000)
    expect(api.check).not.toHaveBeenCalled()
    w.unmount()
  })

  it('discovers a binding finished elsewhere in the background and stops after leaving', async () => {
    api.get.mockResolvedValueOnce(unbound()).mockResolvedValue(bound())
    const w = render(); await flushPromises()
    expect(w.find('[data-test="unbound"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(15000); await flushPromises()
    expect(w.find('[data-test="binding"]').exists()).toBe(true)
    w.unmount()
    const calls = api.get.mock.calls.length
    window.dispatchEvent(new Event('focus'))
    await vi.advanceTimersByTimeAsync(30000)
    expect(api.get).toHaveBeenCalledTimes(calls)
  })
})

describe('one-tap authorization link from the private chat', () => {
  const originalUA = navigator.userAgent
  function arrive(ua: string) {
    Object.defineProperty(window.navigator, 'userAgent', { value: ua, configurable: true })
    window.history.replaceState(null, '', '/my-wecom?authorize=1')
  }
  afterEach(() => {
    Object.defineProperty(window.navigator, 'userAgent', { value: originalUA, configurable: true })
    window.history.replaceState(null, '', '/')
    vi.restoreAllMocks()
  })

  it('jumps straight to the WeCom confirmation page inside WeCom on a phone', async () => {
    arrive('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148 wxwork/5.0.10')
    api.get.mockResolvedValue(unbound())
    api.startScan.mockResolvedValue(unbound({ scan: scan() }))
    const replace = vi.fn()
    vi.spyOn(window, 'location', 'get').mockReturnValue({ ...window.location, replace, search: '?authorize=1', pathname: '/my-wecom', hash: '' } as Location)
    const w = render(); await flushPromises()
    expect(api.startScan).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith(QR_URL)
    // 跳走之后不再轮询，也不再画二维码。
    expect(api.pollScan).not.toHaveBeenCalled()
    expect(document.querySelector('[data-test="bind-redirecting"]')).not.toBeNull()
    w.unmount()
  })

  it('shows the QR code on a desktop and drops the parameter from the address', async () => {
    arrive('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) wxwork/5.0.10')
    api.get.mockResolvedValue(unbound())
    api.startScan.mockResolvedValue(unbound({ scan: scan() }))
    const w = render(); await flushPromises()
    expect(window.location.search).toBe('')
    expect(api.startScan).toHaveBeenCalledTimes(1)
    expect(document.querySelector('[data-test="bind-redirecting"]')).toBeNull()
    w.unmount()
  })

  it('does nothing extra when the member is already bound', async () => {
    arrive('Mozilla/5.0 (iPhone) Mobile wxwork/5.0.10')
    api.get.mockResolvedValue(bound())
    const w = render(); await flushPromises()
    expect(api.startScan).not.toHaveBeenCalled()
    w.unmount()
  })
})
