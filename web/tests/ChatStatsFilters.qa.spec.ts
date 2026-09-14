import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  chatLogs: {
    list: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 }),
    get: vi.fn(),
    stats: vi.fn().mockResolvedValue({ total: 0, by_status: {}, avg_latency_ms: null, tokens: { input: 0, output: 0, cache_read: 0, cache_creation: 0 }, by_bot: [] }),
  },
  bots: { list: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}))

import { chatLogs } from '@/api/admin'
import { i18n } from '@/i18n'
import ChatLogsView from '@/views/ChatLogsView.vue'

// Regression: live QA showed 1 filtered record but 20 records in statistics.
it('keeps the entered keyword when opening statistics', async () => {
  setActivePinia(createPinia())
  const wrapper = mount(ChatLogsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  await wrapper.get('[data-test="filter-keyword"] input').setValue('QA-0914-C')
  await wrapper.get('#tab-stats').trigger('click')
  await flushPromises()
  expect(chatLogs.stats).toHaveBeenLastCalledWith(expect.objectContaining({ keyword: 'QA-0914-C' }))
  wrapper.unmount()
})
