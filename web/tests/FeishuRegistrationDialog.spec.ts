import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,qr') } }))
vi.mock('@/api/feishuApps', () => ({
  feishuApps: { startRegistration: vi.fn(), registration: vi.fn(), cancelRegistration: vi.fn() },
}))

import QRCode from 'qrcode'
import { feishuApps, type FeishuRegistration } from '@/api/feishuApps'
import FeishuRegistrationDialog from '@/components/feishuApp/FeishuRegistrationDialog.vue'
import { i18n } from '@/i18n'

const URL = 'https://open.feishu.cn/page/launcher?user_code=AB-CD'
function reg(over: Partial<FeishuRegistration> = {}): FeishuRegistration {
  return { id: 'reg1', purpose: 'create', status: 'pending', bot_id: null, app_id: null, url: URL, expires_at: '', retry_after: 2, error: null, created_at: null, reused: false, ...over }
}
function mountDialog(props: Record<string, unknown> = {}) {
  return mount(FeishuRegistrationDialog, {
    props: { purpose: 'create', visible: true, preset: { name: '销售助手', description: '卖货' }, ...props },
    global: { plugins: [ElementPlus, i18n] },
    attachTo: document.body,
  })
}

describe('FeishuRegistrationDialog', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    vi.mocked(feishuApps.startRegistration).mockReset()
    vi.mocked(feishuApps.registration).mockReset()
    vi.mocked(feishuApps.cancelRegistration).mockReset()
  })
  afterEach(() => { vi.useRealTimers(); document.body.innerHTML = '' })

  it('renders the confirmation link as a QR code and polls at the server pace until created', async () => {
    vi.mocked(feishuApps.startRegistration).mockResolvedValue(reg())
    vi.mocked(feishuApps.registration)
      .mockResolvedValueOnce(reg({ retry_after: 5 }))
      .mockResolvedValueOnce(reg({ status: 'succeeded', url: null, app_id: 'cli_1' }))
    const wrapper = mountDialog()
    await flushPromises()
    expect(feishuApps.startRegistration).toHaveBeenCalledWith({ purpose: 'create', reuse: true, name: '销售助手', description: '卖货' })
    expect(QRCode.toDataURL).toHaveBeenCalledWith(URL, expect.anything())
    expect(document.querySelector('[data-test="registration-qr"]')).not.toBeNull()
    expect(document.querySelector('[data-test="registration-link"]')?.getAttribute('href')).toBe(URL)

    await vi.advanceTimersByTimeAsync(1999)
    expect(feishuApps.registration).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    expect(feishuApps.registration).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(feishuApps.registration).toHaveBeenCalledTimes(2)
    expect(wrapper.emitted('succeeded')?.[0]?.[0]).toMatchObject({ id: 'reg1', app_id: 'cli_1' })
    wrapper.unmount()
  })

  it('asks before reusing an unused app and can start a fresh scan instead', async () => {
    vi.mocked(feishuApps.startRegistration)
      .mockResolvedValueOnce(reg({ status: 'succeeded', url: null, app_id: 'cli_old', reused: true }))
      .mockResolvedValueOnce(reg({ id: 'reg2' }))
    const wrapper = mountDialog()
    await flushPromises()
    expect(wrapper.emitted('succeeded')).toBeFalsy()
    expect(document.body.textContent).toContain('cli_old')
    ;(document.querySelector('[data-test="registration-new"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(feishuApps.startRegistration).toHaveBeenLastCalledWith(expect.objectContaining({ reuse: false }))
    expect(document.querySelector('[data-test="registration-qr"]')).not.toBeNull()
    wrapper.unmount()

    vi.mocked(feishuApps.startRegistration).mockResolvedValueOnce(reg({ status: 'succeeded', url: null, app_id: 'cli_old', reused: true }))
    const reuse = mountDialog()
    await flushPromises()
    ;(document.querySelector('[data-test="registration-reuse"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(reuse.emitted('succeeded')?.[0]?.[0]).toMatchObject({ id: 'reg1', app_id: 'cli_old' })
    reuse.unmount()
  })

  it('cancels a pending scan when closed and stops polling', async () => {
    vi.mocked(feishuApps.startRegistration).mockResolvedValue(reg())
    const wrapper = mountDialog()
    await flushPromises()
    await wrapper.setProps({ visible: false })
    await flushPromises()
    wrapper.findComponent({ name: 'ElDialog' }).vm.$emit('close')
    await flushPromises()
    expect(feishuApps.cancelRegistration).toHaveBeenCalledWith('reg1')
    await vi.advanceTimersByTimeAsync(10000)
    expect(feishuApps.registration).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('offers a new QR code after the scan was denied', async () => {
    vi.mocked(feishuApps.startRegistration)
      .mockResolvedValueOnce(reg({ status: 'denied', url: null, error: 'denied' }))
      .mockResolvedValueOnce(reg({ id: 'reg2' }))
    const wrapper = mountDialog()
    await flushPromises()
    expect(document.body.textContent).toContain(i18n.global.t('feishuApp.registrationStatus.denied'))
    ;(document.querySelector('[data-test="registration-retry"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(feishuApps.startRegistration).toHaveBeenLastCalledWith(expect.objectContaining({ reuse: false }))
    wrapper.unmount()
  })

  it('updates the bound app of an existing employee', async () => {
    vi.mocked(feishuApps.startRegistration).mockResolvedValue(reg({ purpose: 'update', status: 'consumed', url: null, app_id: 'cli_1', bot_id: 'b1' }))
    const wrapper = mountDialog({ purpose: 'update', botId: 'b1', preset: undefined })
    await flushPromises()
    expect(feishuApps.startRegistration).toHaveBeenCalledWith({ purpose: 'update', bot_id: 'b1' })
    expect(wrapper.emitted('succeeded')).toBeTruthy()
    wrapper.unmount()
  })
})
