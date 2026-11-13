import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/assistantsAdmin', () => ({ assistantsAdmin: { list: vi.fn(), deprovision: vi.fn() } }))

import { assistantsAdmin, type AssistantRow } from '@/api/assistantsAdmin'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'
import { router } from '@/router'
import AssistantsView from '@/views/AssistantsView.vue'

const t = i18n.global.t
const row = (over: Partial<AssistantRow>): AssistantRow => ({
  bot_id: 'b1', state: 'active', enabled: true, created_at: '2026-11-10T00:00:00Z',
  owner: { id: 'u1', login_name: 'zhangsan', display_name: '张三' },
  relay: { id: 'r1', name: 'n1/claude' }, node: { id: 'n1', name: 'pool-1', online: true },
  lifecycle: null, on_shift: true,
  cold_start: { state: 'in_progress', step: 'focus', outcome: null, started_at: '2026-11-13T01:00:00Z', done_at: null },
  directories_pending_removal: 0,
  watch: { state: 'backoff', last_ok_at: '2026-11-13T02:00:00Z', last_error: 'rate_limited' },
  turns: { today: 12, budget: 200 }, inbox_pending: 4, jobs_active: 2, ...over,
})
const render = () => mount(AssistantsView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(assistantsAdmin.list).mockReset().mockResolvedValue({
    items: [
      row({}),
      row({ bot_id: 'b2', state: 'deprovisioned', enabled: false, owner: { id: 'u2', login_name: null, display_name: '李四' }, node: null, relay: null, watch: null, turns: null, on_shift: false, cold_start: null }),
    ],
  })
  vi.mocked(assistantsAdmin.deprovision).mockReset().mockResolvedValue({ bot_id: 'b1', state: 'deprovisioning' })
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
})

describe('personal assistants (admin)', () => {
  it('is open to platform administrators only', () => {
    expect(MENU.find((item) => item.key === 'assistants')).toMatchObject({ path: '/assistants', roles: ['platform_admin'] })
    const route = router.resolve({ name: 'assistants' })
    expect(route.path).toBe('/assistants')
    expect(route.meta.roles).toEqual(['platform_admin'])
  })

  it('shows who, where, health and counters, and says it is metadata only', async () => {
    const wrapper = render(); await flushPromises()
    const text = wrapper.text()
    expect(text).toContain(t('assistant.admin.intro'))
    for (const expected of ['张三 (zhangsan)', '李四', 'pool-1', '12 / 200', 'rate_limited', 'focus']) expect(text).toContain(expected)
    expect(text).toContain(t('assistant.overview.states.active'))
    expect(text).toContain(t('assistant.overview.states.deprovisioned'))
    expect(text).toContain(t('assistant.overview.coldStartStates.in_progress'))
    expect(text).toContain(t('assistant.overview.watchStates.backoff'))
    wrapper.unmount()
  })

  it('says so when nobody has an assistant', async () => {
    vi.mocked(assistantsAdmin.list).mockResolvedValue({ items: [] })
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.admin.empty'))
    wrapper.unmount()
  })

  it('shuts an assistant down only after the owner login name is typed', async () => {
    const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: 'zhangsan', action: 'confirm' } as never)
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="deprovision-b2"]').exists()).toBe(false)
    await wrapper.get('[data-test="deprovision-b1"]').trigger('click'); await flushPromises()
    expect(prompt.mock.calls[0][0]).toBe(t('assistant.admin.deprovisionPrompt', { name: 'zhangsan' }))
    const options = prompt.mock.calls[0][2] as { inputValidator: (value: string) => boolean | string }
    expect(options.inputValidator('zhangsan')).toBe(true)
    expect(options.inputValidator('张三')).toBe(t('assistant.admin.deprovisionMismatch'))
    expect(assistantsAdmin.deprovision).toHaveBeenCalledWith('b1')
    expect(assistantsAdmin.list).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('does nothing when the confirmation is dismissed', async () => {
    vi.spyOn(ElMessageBox, 'prompt').mockRejectedValue('cancel')
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="deprovision-b1"]').trigger('click'); await flushPromises()
    expect(assistantsAdmin.deprovision).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
