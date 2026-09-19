import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,qr') } }))
vi.mock('@/api/wecomBots', () => ({
  wecomBots: { startProvision: vi.fn(), provision: vi.fn(), cancelProvision: vi.fn() },
}))

import QRCode from 'qrcode'
import { wecomBots, type WecomProvision } from '@/api/wecomBots'
import WecomProvisionDialog from '@/components/wecomBot/WecomProvisionDialog.vue'
import { i18n } from '@/i18n'

const URL = 'https://work.weixin.qq.com/ai/qc/c?s=Ab12Cd34Ef56Gh78&hide_more_btn=true&for_native=true'
function prov(over: Partial<WecomProvision> = {}): WecomProvision {
  return {
    id: 'p1', status: 'pending', bot_id: null, wecom_bot_id: null, url: URL, upstream_status: null, verified: false,
    expires_at: '2026-09-19T08:05:00Z', retry_after: 3, error: null, created_at: null, reused: false, ...over,
  }
}
function mountDialog() {
  return mount(WecomProvisionDialog, {
    props: { visible: true },
    global: { plugins: [ElementPlus, i18n] },
    attachTo: document.body,
  })
}
const text = () => document.body.textContent ?? ''

describe('WecomProvisionDialog', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    vi.mocked(wecomBots.startProvision).mockReset()
    vi.mocked(wecomBots.provision).mockReset()
    vi.mocked(wecomBots.cancelProvision).mockReset()
  })
  afterEach(() => { vi.useRealTimers(); document.body.innerHTML = '' })

  it('draws the QR code without a forwardable link and polls until the bot is verified', async () => {
    vi.mocked(wecomBots.startProvision).mockResolvedValue(prov())
    vi.mocked(wecomBots.provision)
      .mockResolvedValueOnce(prov({ upstream_status: 'pending' }))
      .mockResolvedValueOnce(prov({ status: 'succeeded', url: null, wecom_bot_id: 'aib-1', verified: true }))
    const wrapper = mountDialog()
    await flushPromises()
    expect(wecomBots.startProvision).toHaveBeenCalledWith({ reuse: true })
    expect(QRCode.toDataURL).toHaveBeenCalledWith(URL, expect.anything())
    expect(document.querySelector('[data-test="provision-qr"]')).not.toBeNull()
    expect(document.querySelector('[data-test="provision-secret-warning"]')).not.toBeNull()
    expect(document.querySelector('.provision a')).toBeNull()
    expect(text()).not.toContain(URL)
    // 员工机器人不该点「确认授权」：扫码时就提醒跳过，不再引导去授权。
    expect(document.querySelector('[data-test="provision-skip-authorize"]')?.textContent).toContain(i18n.global.t('wecomBot.skipAuthorizeHint'))
    expect(text()).not.toContain(i18n.global.t('wecomBot.usageModeHint'))

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(wecomBots.provision).toHaveBeenCalledTimes(1)
    expect(document.querySelector('[data-test="provision-progress"]')?.textContent).toContain(i18n.global.t('wecomBot.scanned'))
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(wrapper.emitted('succeeded')?.[0]?.[0]).toMatchObject({ id: 'p1', wecom_bot_id: 'aib-1' })
    // 建好后提醒改成「多人使用」，并撤掉误点的「确认授权」。
    expect(document.querySelector('[data-test="provision-created"]')?.textContent).toContain(i18n.global.t('wecomBot.usageModeHint'))
    expect(document.querySelector('[data-test="provision-skip-authorize"]')).toBeNull()
    wrapper.unmount()
  })

  it('stops to explain a failed verification and lets the user retry creation', async () => {
    vi.mocked(wecomBots.startProvision).mockResolvedValue(
      prov({ status: 'succeeded', url: null, wecom_bot_id: 'aib-1', verified: false, error: 'verify_rejected' }),
    )
    const wrapper = mountDialog()
    await flushPromises()
    expect(wrapper.emitted('succeeded')).toBeFalsy()
    expect(text()).toContain(i18n.global.t('wecomBot.provisionError.verify_rejected'))
    ;(document.querySelector('[data-test="provision-verify"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(wrapper.emitted('succeeded')?.[0]?.[0]).toMatchObject({ id: 'p1', wecom_bot_id: 'aib-1' })
    wrapper.unmount()
  })

  it('asks before reusing an unlinked bot and can scan again instead', async () => {
    vi.mocked(wecomBots.startProvision)
      .mockResolvedValueOnce(prov({ status: 'succeeded', url: null, wecom_bot_id: 'aib-old', verified: true, reused: true }))
      .mockResolvedValueOnce(prov({ id: 'p2' }))
    const wrapper = mountDialog()
    await flushPromises()
    expect(wrapper.emitted('succeeded')).toBeFalsy()
    expect(text()).toContain('aib-old')
    ;(document.querySelector('[data-test="provision-new"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(wecomBots.startProvision).toHaveBeenLastCalledWith({ reuse: false })
    expect(document.querySelector('[data-test="provision-qr"]')).not.toBeNull()
    wrapper.unmount()

    vi.mocked(wecomBots.startProvision).mockResolvedValueOnce(
      prov({ status: 'succeeded', url: null, wecom_bot_id: 'aib-old', verified: true, reused: true }),
    )
    const reuse = mountDialog()
    await flushPromises()
    ;(document.querySelector('[data-test="provision-reuse"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(reuse.emitted('succeeded')?.[0]?.[0]).toMatchObject({ id: 'p1', wecom_bot_id: 'aib-old', reused: false })
    reuse.unmount()
  })

  it('cancels a pending scan when closed and stops polling', async () => {
    vi.mocked(wecomBots.startProvision).mockResolvedValue(prov())
    const wrapper = mountDialog()
    await flushPromises()
    wrapper.findComponent({ name: 'ElDialog' }).vm.$emit('close')
    await flushPromises()
    expect(wecomBots.cancelProvision).toHaveBeenCalledWith('p1')
    await vi.advanceTimersByTimeAsync(10000)
    expect(wecomBots.provision).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('offers a new QR code after the old one expired', async () => {
    vi.mocked(wecomBots.startProvision)
      .mockResolvedValueOnce(prov({ status: 'expired', url: null, error: 'qr_expired' }))
      .mockResolvedValueOnce(prov({ id: 'p2' }))
    const wrapper = mountDialog()
    await flushPromises()
    expect(text()).toContain(i18n.global.t('wecomBot.provisionStatus.expired'))
    expect(text()).toContain(i18n.global.t('wecomBot.provisionError.qr_expired'))
    ;(document.querySelector('[data-test="provision-retry"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(wecomBots.startProvision).toHaveBeenLastCalledWith({ reuse: false })
    wrapper.unmount()
  })
})
