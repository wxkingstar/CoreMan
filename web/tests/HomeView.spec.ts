import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { i18n } from '@/i18n'
import HomeView from '@/views/HomeView.vue'
import { useAuthStore } from '@/stores/auth'
import { bots, runtime } from '@/api/admin'
import { skills } from '@/api/skills'
import { cron } from '@/api/cron'
import { statistics } from '@/api/statistics'
vi.mock('@/api/admin', () => ({ bots: { list: vi.fn() }, runtime: { queue: vi.fn().mockResolvedValue({ outbox_failed: 0 }) } }))
vi.mock('@/api/skills', () => ({ skills: { approvals: vi.fn() } }))
vi.mock('@/api/cron', () => ({ cron: { list: vi.fn() } }))
vi.mock('@/api/statistics', async () => ({ ...await vi.importActual('@/api/statistics'), statistics: { get: vi.fn() } }))
beforeEach(() => { setActivePinia(createPinia()); vi.resetAllMocks(); useAuthStore().user = { id: 'u', role: 'member', display_name: 'User' } as never })
it('uses endpoint totals, preserves unmetered cost, and hides restricted shortcuts', async () => {
  vi.mocked(bots.list).mockResolvedValue({ items: [], total: 128 } as never)
  vi.mocked(cron.list).mockResolvedValue({ items: [], total: 71 } as never)
  vi.mocked(statistics.get).mockResolvedValue({ start: '2026-09-01', end: '2026-09-13', timezone: 'Asia/Shanghai', total: { cost_usd: null, cost_usd_measured: 0, messages: 27, input_tokens: 900, output_tokens: 100 }, daily: [{ day: '2026-09-01', messages: 27, input_tokens: 900, output_tokens: 100 }] } as never)
  const wrapper = mount(HomeView, { global: { plugins: [ElementPlus, i18n], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
  await flushPromises()
  expect(wrapper.text()).toContain('128')
  expect(wrapper.text()).toContain('71')
  expect(wrapper.text()).toContain(i18n.global.t('statistics.unknown'))
  expect(wrapper.text()).toContain(i18n.global.t('statistics.coverage', { n: 0, total: 27 }))
  expect(wrapper.text()).not.toContain(i18n.global.t('menu.skillApprovals'))
  // 首页趋势图画的是每日 Token 合计，不是消息数。
  expect(wrapper.get('.mini-chart rect title').text()).toBe('2026-09-01: 1,000')
  expect(wrapper.get('.usage-summary').text()).toContain('1,000')
  expect(runtime.queue).not.toHaveBeenCalled()
  expect(skills.approvals).not.toHaveBeenCalled()
  expect(bots.list).toHaveBeenCalledWith({ scope: 'all', enabled: true, page: 1, per_page: 1 })
  wrapper.unmount()
})
it('isolates request failures without presenting a failed count as zero', async () => {
  vi.mocked(bots.list).mockRejectedValue(new Error('employees unavailable'))
  vi.mocked(cron.list).mockResolvedValue({ items: [], total: 7 } as never)
  vi.mocked(statistics.get).mockRejectedValue(new Error('usage unavailable'))
  const wrapper = mount(HomeView, { global: { plugins: [ElementPlus, i18n], stubs: { RouterLink: true } } })
  await flushPromises()
  expect(wrapper.text()).toContain('employees unavailable')
  expect(wrapper.text()).toContain('usage unavailable')
  expect(wrapper.text()).toContain('7')
  expect(wrapper.text()).not.toContain('$0')
  wrapper.unmount()
})
