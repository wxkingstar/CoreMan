import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/infrastructure', () => ({ systemAccess: { list: vi.fn(), decide: vi.fn() } }))

import { systemAccess, type SystemAccessRequest } from '@/api/infrastructure'
import { i18n } from '@/i18n'
import SystemAccessRequestsPanel from '@/views/SystemAccessRequestsPanel.vue'

const pending: SystemAccessRequest = {
  id: 'r-1', bot_id: 'bot-1', bot_name: '李四的助理', system_key: 'orders', system_name: '订单系统',
  write: true, reason: '查客户订单状态', requester: '李四', status: 'pending',
  decision_note: null, created_at: '2026-11-18T00:00:00Z', decided_at: null,
}
const approved: SystemAccessRequest = { ...pending, id: 'r-2', status: 'approved', decided_at: '2026-11-18T01:00:00Z' }
const page = (items: SystemAccessRequest[]) => ({ items, total: items.length, page: 1, per_page: 50 })
const t = i18n.global.t
const plugins = [ElementPlus, i18n]

describe('SystemAccessRequestsPanel', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { vi.restoreAllMocks() })

  it('lists pending requests with the system and the reason', async () => {
    vi.mocked(systemAccess.list).mockResolvedValue(page([pending, approved]))
    const wrapper = mount(SystemAccessRequestsPanel, { global: { plugins }, props: { active: true } })
    await flushPromises()
    expect(systemAccess.list).toHaveBeenCalledWith('pending', 1)
    const text = wrapper.text()
    expect(text).toContain('订单系统')
    expect(text).toContain('查客户订单状态')
    expect(text).toContain('李四的助理')
    expect(text).toContain(t('systemAccess.write'))
    expect(wrapper.find('[data-test="approve-r-1"]').exists()).toBe(true)
    // 处理过的没有操作按钮。
    expect(wrapper.find('[data-test="approve-r-2"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('does not load until its tab is shown, then switches to all', async () => {
    vi.mocked(systemAccess.list).mockResolvedValue(page([]))
    const wrapper = mount(SystemAccessRequestsPanel, { global: { plugins }, props: { active: false }, attachTo: document.body })
    await flushPromises()
    expect(systemAccess.list).not.toHaveBeenCalled()
    await wrapper.setProps({ active: true }); await flushPromises()
    expect(systemAccess.list).toHaveBeenCalledWith('pending', 1)
    expect(wrapper.text()).toContain(t('systemAccess.empty'))
    const all = [...wrapper.findAll('[data-test="access-filter"] label')].find(el => el.text().includes(t('systemAccess.all')))!
    ;(all.element as HTMLElement).click(); await flushPromises()
    expect(systemAccess.list).toHaveBeenLastCalledWith('all', 1)
    wrapper.unmount()
  })

  it('approves after confirmation and reloads', async () => {
    vi.mocked(systemAccess.list).mockResolvedValue(page([pending]))
    vi.mocked(systemAccess.decide).mockResolvedValue({ id: 'r-1', status: 'approved' })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const wrapper = mount(SystemAccessRequestsPanel, { global: { plugins }, props: { active: true } })
    await flushPromises()
    await wrapper.get('[data-test="approve-r-1"]').trigger('click'); await flushPromises()
    expect(String(confirm.mock.calls[0][0])).toContain('李四的助理')
    expect(systemAccess.decide).toHaveBeenCalledWith('r-1', 'approve', '')
    expect(systemAccess.list).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('rejects with the reason typed in, and does nothing when the prompt is cancelled', async () => {
    vi.mocked(systemAccess.list).mockResolvedValue(page([pending]))
    vi.mocked(systemAccess.decide).mockResolvedValue({ id: 'r-1', status: 'rejected' })
    const prompt = vi.spyOn(ElMessageBox, 'prompt')
      .mockRejectedValueOnce('cancel')
      .mockResolvedValueOnce({ value: '理由', action: 'confirm' } as never)
    const wrapper = mount(SystemAccessRequestsPanel, { global: { plugins }, props: { active: true } })
    await flushPromises()
    await wrapper.get('[data-test="reject-r-1"]').trigger('click'); await flushPromises()
    expect(prompt).toHaveBeenCalledTimes(1)
    expect(systemAccess.decide).not.toHaveBeenCalled()
    await wrapper.get('[data-test="reject-r-1"]').trigger('click'); await flushPromises()
    expect(systemAccess.decide).toHaveBeenCalledWith('r-1', 'reject', '理由')
    expect(systemAccess.list).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
