import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({ auditLogs: { list: vi.fn().mockResolvedValue({ items: [{ id: 9, actor_id: null, actor_login: 'zhangsan', action: 'bot.update', target_type: 'bot', target_id: 'b1', diff: { name: ['a', 'b'] }, ip: '10.0.0.1', created_at: '2026-09-10T00:00:00Z' }], total: 1, page: 1, per_page: 50 }) } }))

import { auditLogs } from '@/api/admin'
import { i18n } from '@/i18n'
import AuditLogsView from '@/views/AuditLogsView.vue'

describe('AuditLogsView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('lists logs and applies action prefix filter', async () => {
    const wrapper = mount(AuditLogsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).toContain('bot.update')
    expect(wrapper.text()).toContain('2026-09-10 08:00:00')
    ;(wrapper.vm as unknown as { paged: { filters: { action: string }; load: () => Promise<void> } }).paged.filters.action = 'bot.'
    await (wrapper.vm as unknown as { paged: { load: () => Promise<void> } }).paged.load()
    expect(auditLogs.list).toHaveBeenLastCalledWith(expect.objectContaining({ action: 'bot.' }))
  })

  // 空筛选项不能进 query，时间范围必须转成 ISO 字符串（Date 对象被 axios 序列化成本地格式，后端 422）。
  it('omits blank filters and converts the picked range to ISO strings', async () => {
    vi.mocked(auditLogs.list).mockClear()
    const wrapper = mount(AuditLogsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(auditLogs.list).toHaveBeenLastCalledWith({ page: 1, per_page: 50 })
    const vm = wrapper.vm as unknown as { range: [Date, Date] | null; paged: { load: () => Promise<void> } }
    vm.range = [new Date('2026-09-01T00:00:00Z'), new Date('2026-09-10T00:00:00Z')]
    await flushPromises()
    await vm.paged.load()
    expect(auditLogs.list).toHaveBeenLastCalledWith({ page: 1, per_page: 50, since: '2026-09-01T00:00:00.000Z', until: '2026-09-10T00:00:00.000Z' })
  })
})
