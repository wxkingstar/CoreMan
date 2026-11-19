import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { overview: vi.fn(), shift: vi.fn(), reset: vi.fn() },
}))
// 各分区有自己的用例，这里换成只带标记的替身：壳只管概览、空状态与标签页。
const boardLoad = vi.hoisted(() => vi.fn())
vi.mock('@/components/assistant/AssistantBoard.vue', () => ({ default: { template: '<div data-test="pane-board" />', methods: { load: boardLoad } } }))
vi.mock('@/components/assistant/AssistantJobs.vue', () => ({ default: { template: '<div data-test="pane-jobs" />' } }))
vi.mock('@/components/assistant/AssistantFollowups.vue', () => ({ default: { template: '<div data-test="pane-followups" />' } }))
vi.mock('@/components/assistant/AssistantRecent.vue', () => ({ default: { template: '<div data-test="pane-recent" />' } }))
vi.mock('@/components/assistant/AssistantNotes.vue', () => ({ default: { template: '<div data-test="pane-notes" />' } }))
vi.mock('@/components/assistant/AssistantSettings.vue', () => ({
  default: { props: ['name', 'runtime'], emits: ['deprovisioned'], template: '<div data-test="pane-settings">{{ name }} / {{ runtime }}</div>' },
}))

import { myAssistant, type AssistantAbsent, type AssistantOverview, type AssistantPrefs } from '@/api/myAssistant'
import AssistantSettings from '@/components/assistant/AssistantSettings.vue'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'
import { router } from '@/router'
import MyAssistantView from '@/views/MyAssistantView.vue'

const t = i18n.global.t
const absent: AssistantAbsent = {
  exists: false, eligible: true, reason: null,
  how_to: { app_name: '个人助理', steps: ['open_private_chat', 'accept_offer', 'authorize'] }, lifecycle: null,
}
const overview: AssistantOverview = {
  exists: true, bot_id: 'b1', name: '个人助理', state: 'active', runtime: { name: 'pool-1', online: true }, paused_reason: null,
  on_shift: true, always_on: false, shift_override: null, shift_override_until: null, cold_start: { state: 'in_progress', step: 'focus', outcome: null }, turns: { today: 3, budget: 50 },
  watch: { state: 'polling', last_ok_at: '2026-11-13T02:00:00Z', last_error: null },
  lifecycle: null, content_locked: false, approval_system: { name: null, url: null },
}
const render = () => mount(MyAssistantView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  // 分区记在地址栏里：每条用例从总表开始。
  window.history.replaceState(null, '', '/my-assistant')
  vi.mocked(myAssistant.overview).mockReset().mockResolvedValue(overview)
})

describe('My assistant page', () => {
  it('is a personal page every signed-in user can open', () => {
    const entry = MENU.find((item) => item.key === 'myAssistant')
    expect(entry?.path).toBe('/my-assistant')
    expect(entry?.roles).toBeUndefined()
    const route = router.resolve({ name: 'my-assistant' })
    expect(route.path).toBe('/my-assistant')
    expect(route.meta.roles).toBeUndefined()
  })

  it('tells someone without an assistant how to get one', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue(absent)
    const wrapper = render(); await flushPromises()
    const empty = wrapper.get('[data-test="assistant-empty"]')
    expect(empty.text()).toContain(t('assistant.empty.title'))
    expect(empty.text()).toContain(t('assistant.empty.app', { name: '个人助理' }))
    for (const step of absent.how_to.steps) expect(empty.text()).toContain(t(`assistant.empty.steps.${step}`))
    expect(wrapper.find('[data-test="assistant-overview"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="pane-board"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('gives the reason instead of the steps when the member is out of scope', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...absent, eligible: false, reason: 'not_listed', how_to: { app_name: null, steps: absent.how_to.steps } })
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.empty.reasons.not_listed'))
    expect(wrapper.text()).not.toContain(t('assistant.empty.steps.accept_offer'))
    wrapper.unmount()
  })

  it('says setup is in progress while a flow is running', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...absent, lifecycle: { action: 'provision', step: 'offered' } })
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.empty.inProgress'))
    expect(wrapper.text()).not.toContain(t('assistant.empty.steps.accept_offer'))
    wrapper.unmount()
  })

  it('shows state, runtime, shift, cold start, turns and watch health', async () => {
    const wrapper = render(); await flushPromises()
    const head = wrapper.get('[data-test="assistant-overview"]').text()
    expect(head).toContain(t('assistant.overview.states.active'))
    expect(head).toContain('pool-1')
    expect(head).toContain(t('assistant.overview.online'))
    expect(head).toContain(t('assistant.overview.onShift'))
    expect(head).toContain(t('assistant.overview.coldStartStates.in_progress'))
    expect(head).toContain(t('assistant.overview.turnsValue', { n: 3, total: 50 }))
    expect(head).toContain(t('assistant.overview.watchStates.polling'))
    expect(wrapper.find('[data-test="assistant-locked"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows why turns cannot run and that content is unreadable', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue({
      ...overview, runtime: null, paused_reason: 'runtime_offline', on_shift: false, watch: null, content_locked: true,
      cold_start: { state: 'done', step: null, outcome: 'completed' },
    })
    const wrapper = render(); await flushPromises()
    const head = wrapper.get('[data-test="assistant-overview"]').text()
    expect(head).toContain(t('assistant.overview.runtimeNone'))
    expect(head).toContain(t('assistant.overview.paused', { detail: 'runtime_offline' }))
    expect(head).toContain(t('assistant.overview.offShift'))
    // 冷启动做完就不再占一格。
    expect(wrapper.find('[data-test="cold-start"]').exists()).toBe(false)
    expect(head).toContain(t('assistant.overview.watchNone'))
    expect(wrapper.get('[data-test="assistant-locked"]').text()).toContain(t('assistant.page.locked'))
    wrapper.unmount()
  })

  it('mounts sections lazily and reloads the overview after shutdown starts', async () => {
    const wrapper = render(); await flushPromises()
    for (const name of ['board', 'jobs', 'followups', 'recent', 'notes', 'settings']) expect(wrapper.text()).toContain(t(`assistant.page.tabs.${name}`))
    expect(wrapper.find('[data-test="pane-board"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="pane-settings"]').exists()).toBe(false)
    ;(wrapper.vm as unknown as { tab: string }).tab = 'settings'
    await nextTick(); await flushPromises()
    const settings = wrapper.get('[data-test="pane-settings"]')
    expect(settings.text()).toBe('个人助理 / pool-1')
    wrapper.findComponent(AssistantSettings).vm.$emit('deprovisioned')
    await flushPromises()
    expect(myAssistant.overview).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('refreshes the overview together with the open section', async () => {
    boardLoad.mockReset().mockResolvedValue(undefined)
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="refresh"]').trigger('click'); await flushPromises()
    expect(myAssistant.overview).toHaveBeenCalledTimes(2)
    expect(boardLoad).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('opens the section named in the address and keeps it there when switching', async () => {
    window.history.replaceState(null, '', '/my-assistant?tab=followups')
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="pane-followups"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="pane-board"]').exists()).toBe(false)
    ;(wrapper.vm as unknown as { tab: string }).tab = 'notes'
    await nextTick()
    expect(window.location.search).toBe('?tab=notes')
    ;(wrapper.vm as unknown as { tab: string }).tab = 'board'
    await nextTick()
    expect(window.location.search).toBe('')
    wrapper.unmount()
  })

  it('clocks in and out from the page and reloads the overview', async () => {
    vi.mocked(myAssistant.shift).mockReset().mockResolvedValue({} as AssistantPrefs)
    vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    // 按工作时段时没有「恢复」按钮，也没有到期时刻。
    expect(wrapper.find('[data-test="shift-auto"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="shift-until"]').exists()).toBe(false)
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...overview, on_shift: false, shift_override: 'off', shift_override_until: '2026-11-14T01:30:00Z' })
    await wrapper.get('[data-test="shift-off"]').trigger('click'); await flushPromises()
    expect(myAssistant.shift).toHaveBeenCalledWith('off')
    expect(myAssistant.overview).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-test="shift-state"]').text()).toBe(t('assistant.overview.offShift'))
    expect(wrapper.find('[data-test="shift-until"]').exists()).toBe(true)
    await wrapper.get('[data-test="shift-auto"]').trigger('click'); await flushPromises()
    expect(myAssistant.shift).toHaveBeenLastCalledWith('auto')
    await wrapper.get('[data-test="shift-on"]').trigger('click'); await flushPromises()
    expect(myAssistant.shift).toHaveBeenLastCalledWith('on')
    wrapper.unmount()
  })

  it('shows around the clock and offers only clocking out or going back to it', async () => {
    vi.mocked(myAssistant.shift).mockReset().mockResolvedValue({} as AssistantPrefs)
    vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...overview, always_on: true })
    const wrapper = render(); await flushPromises()
    expect(wrapper.get('[data-test="shift-state"]').text()).toBe(t('assistant.overview.alwaysOn'))
    // 全天候时本来就在班：没有「上班」。
    expect(wrapper.find('[data-test="shift-on"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="shift-auto"]').exists()).toBe(false)
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...overview, always_on: true, on_shift: false, shift_override: 'off', shift_override_until: '2026-11-16T01:30:00Z' })
    await wrapper.get('[data-test="shift-off"]').trigger('click'); await flushPromises()
    expect(wrapper.get('[data-test="shift-state"]').text()).toBe(t('assistant.overview.offShift'))
    expect(wrapper.get('[data-test="shift-auto"]').text()).toBe(t('assistant.overview.shiftAlwaysOn'))
    await wrapper.get('[data-test="shift-auto"]').trigger('click'); await flushPromises()
    expect(myAssistant.shift).toHaveBeenLastCalledWith('auto')
    wrapper.unmount()
  })

  it('offers no shift switch while the assistant is not active', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...overview, state: 'deprovisioning', on_shift: false })
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="shift-on"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="shift-off"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('starts fresh after confirming, reports what was cleared and reloads', async () => {
    vi.mocked(myAssistant.reset).mockReset().mockResolvedValue({ followups: 5, jobs: 2, approvals: 1, inbox: 7, reports: 0, gaps: 3 })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
    const success = vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    // 取消确认不发请求。
    await wrapper.get('[data-test="assistant-reset"]').trigger('click'); await flushPromises()
    expect(myAssistant.reset).not.toHaveBeenCalled()
    confirm.mockResolvedValueOnce('confirm' as never)
    await wrapper.get('[data-test="assistant-reset"]').trigger('click'); await flushPromises()
    expect(myAssistant.reset).toHaveBeenCalledTimes(1)
    expect(success).toHaveBeenCalledWith(t('assistant.page.resetDone', { n: 5, count: 2, total: 1, value: 7, detail: 3 }))
    expect(myAssistant.overview).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('offers no fresh start while the assistant is not active', async () => {
    vi.mocked(myAssistant.overview).mockResolvedValue({ ...overview, state: 'provisioning' })
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="assistant-reset"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the error when the overview cannot be loaded', async () => {
    vi.mocked(myAssistant.overview).mockRejectedValue(new Error('加载不了'))
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain('加载不了')
    expect(wrapper.find('[data-test="assistant-empty"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
