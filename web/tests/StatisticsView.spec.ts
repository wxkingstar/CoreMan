import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, expect, it, vi } from 'vitest'
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user: { role: 'member' } }) }))
vi.mock('@/api/statistics', async () => {
  const actual = await vi.importActual('@/api/statistics')
  return { ...actual, statistics: { get: vi.fn(), prices: vi.fn() } }
})
import { statistics } from '@/api/statistics'
import StatisticsView from '@/views/StatisticsView.vue'
import { i18n } from '@/i18n'

afterEach(() => vi.unstubAllGlobals())

it('labels missing costs as unmeasured and hides price management from members', async () => {
  let resize: ResizeObserverCallback | undefined
  const disconnect = vi.fn()
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: ResizeObserverCallback) {}
    observe(target: Element) { if (target.classList.contains('chart')) resize = this.callback }
    disconnect = disconnect
  })
  vi.mocked(statistics.get).mockResolvedValue({ start: '2026-09-01', end: '2026-09-02', timezone: 'Asia/Shanghai', total: { messages: 2, users: 1, bots: 1, cost_usd: null, cost_usd_measured: 0, input_tokens: 10, input_tokens_measured: 1 }, daily: [{ day: '2026-09-01', messages: 2, users: 1, bots: 1 }], by_bot: [], by_user: [] } as never)
  const wrapper = mount(StatisticsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
  await flushPromises()
  expect(wrapper.text()).toContain('未计量')
  expect(wrapper.text()).toContain('已计量 0 / 2 条')
  expect(wrapper.text()).not.toContain('$0.000000')
  expect(statistics.prices).not.toHaveBeenCalled()
  expect(wrapper.get('svg[role=img]').attributes('aria-label')).toBe('每日使用趋势')
  resize?.([{ contentRect: { width: 1100 } } as ResizeObserverEntry], {} as ResizeObserver)
  await flushPromises()
  expect(wrapper.get('svg[role=img]').attributes('viewBox')).toBe('0 0 1100 260')
  wrapper.unmount()
  expect(disconnect).toHaveBeenCalled()
})
