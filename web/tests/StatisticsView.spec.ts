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
  vi.mocked(statistics.get).mockResolvedValue({ start: '2026-09-01', end: '2026-09-02', timezone: 'Asia/Shanghai', total: { messages: 2, users: 1, bots: 1, cost_usd: null, cost_usd_measured: 0, input_tokens: 10, input_tokens_measured: 1 }, daily: [{ day: '2026-09-01', messages: 2, users: 1, bots: 1, input_tokens: 1200000, output_tokens: 300000, cache_read_tokens: null, cache_creation_tokens: 0 }, { day: '2026-09-02', messages: 1, users: 1, bots: 1 }], by_bot: [{ id: 'b1', name: 'bot-row', messages: 9 }], by_user: [{ id: 'u1', name: 'Alice', avatar_url: 'https://example.com/a.png', tokens: 123456, messages: 2, bots: 1, cost_usd: null, cost_usd_measured: 0 }] } as never)
  const wrapper = mount(StatisticsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
  await flushPromises()
  expect(wrapper.text()).toContain('未计量')
  expect(wrapper.text()).toContain('已计量 0 / 2 条')
  expect(wrapper.text()).not.toContain('$0')
  expect(statistics.prices).not.toHaveBeenCalled()
  // 用户排行排在第一个并默认展开，显示圆形头像和 Token 合计。
  expect(wrapper.findAll('.el-tabs__item').map(tab => tab.text())).toEqual(['用户排行', 'AI 员工排行'])
  expect(wrapper.get('.el-tab-pane:not([style*="display: none"])').text()).toContain('Alice')
  expect(wrapper.get('.user-cell .el-avatar--circle img').attributes('src')).toBe('https://example.com/a.png')
  expect(wrapper.text()).toContain('123,456')
  expect(wrapper.get('svg[role=img]').attributes('aria-label')).toBe('每日使用趋势')
  // 趋势图默认按每日 Token 合计（四类相加，未计量按 0），纵轴用紧凑数字。
  expect(wrapper.get('.trend-panel .el-radio-button.is-active').text()).toBe('Token 合计')
  expect(wrapper.findAll('svg[role=img] rect title').map(node => node.text())).toEqual(['2026-09-01: 1,500,000', '2026-09-02: 0'])
  expect(wrapper.findAll('svg[role=img] .axis-label').map(node => node.text())).toContain('200万')
  resize?.([{ contentRect: { width: 1100 } } as ResizeObserverEntry], {} as ResizeObserver)
  await flushPromises()
  expect(wrapper.get('svg[role=img]').attributes('viewBox')).toBe('0 0 1100 260')
  wrapper.unmount()
  expect(disconnect).toHaveBeenCalled()
})
