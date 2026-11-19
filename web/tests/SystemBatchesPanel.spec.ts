import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/infrastructure', () => ({ systemBatches: { list: vi.fn(), stop: vi.fn(), items: vi.fn() } }))

import { systemBatches, type SystemBatch, type SystemBatchItem } from '@/api/infrastructure'
import { i18n } from '@/i18n'
import SystemBatchesPanel from '@/views/SystemBatchesPanel.vue'

const base: SystemBatch = {
  id: 'b-running', phase: 'running', pause_reason: null, stopping: false,
  bot_id: 'bot-1', bot_name: 'demo-bot', user_id: 'u-1', user_name: 'Alex',
  system: 'stock', system_name: 'Stock', operation_id: 'updateItem', operation_summary: 'Update an item', risk: 'write',
  count: 10, ok: 3, failed: 1, unknown: 0, skipped: 0, pending: 6, duration_seconds: 1.5,
  created_at: '2026-10-01T00:00:00Z', started_at: '2026-10-01T00:01:00Z', finished_at: null,
}
const paused: SystemBatch = { ...base, id: 'b-paused', pause_reason: 'pause_failures', phase: 'paused', operation_summary: '', ok: 0, failed: 10, pending: 40, count: 50, duration_seconds: null }
const stopping: SystemBatch = { ...base, id: 'b-stopping', stopping: true }
const finished: SystemBatch = { ...base, id: 'b-finished', phase: 'finished', ok: 8, failed: 0, unknown: 1, skipped: 1, pending: 0, duration_seconds: 12.3456, finished_at: '2026-10-01T00:05:00Z' }
const page = (items: SystemBatch[]) => ({ items, total: items.length, page: 1, per_page: 20 })
const t = i18n.global.t
const plugins = [ElementPlus, i18n]

describe('SystemBatchesPanel', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('lists batches with status, progress and non-zero result counts', async () => {
    vi.mocked(systemBatches.list).mockResolvedValue(page([base, paused, stopping, finished]))
    const wrapper = mount(SystemBatchesPanel, { global: { plugins } })
    await flushPromises()
    expect(systemBatches.list).toHaveBeenCalledWith(1, 20)
    const text = wrapper.text()
    expect(text).toContain('demo-bot')
    expect(text).toContain('Alex')
    expect(text).toContain('Stock · Update an item')
    // 没有操作摘要时退回 operation_id
    expect(text).toContain('Stock · updateItem')
    expect(wrapper.get('[data-test="batch-status-b-running"]').text()).toBe(t('systemBatches.phase.running'))
    expect(wrapper.get('[data-test="batch-status-b-paused"]').text()).toContain(t('systemBatches.phase.paused'))
    expect(wrapper.get('[data-test="batch-status-b-paused"]').text()).toContain(t('systemBatches.pauseReason.pause_failures'))
    expect(wrapper.get('[data-test="batch-status-b-stopping"]').text()).toBe(t('systemBatches.stopping'))
    expect(wrapper.get('[data-test="batch-progress-b-running"]').text()).toContain('4 / 10')
    expect(wrapper.get('[data-test="batch-progress-b-finished"]').text()).toContain('10 / 10')
    const running = wrapper.get('[data-test="batch-result-b-running"]').text()
    expect(running).toContain(`${t('systemBatches.results.ok')} 3`)
    expect(running).toContain(`${t('systemBatches.results.failed')} 1`)
    expect(running).not.toContain(t('systemBatches.results.unknown'))
    expect(running).not.toContain(t('systemBatches.results.skipped'))
    const done = wrapper.get('[data-test="batch-result-b-finished"]').text()
    expect(done).toContain(`${t('systemBatches.results.unknown')} 1`)
    expect(done).toContain(`${t('systemBatches.results.skipped')} 1`)
    expect(done).not.toContain(t('systemBatches.results.failed'))
    // 耗时：秒，最多两位小数，小数部分单独一段（小一号、颜色淡）；还没有结果时是 —。
    expect(text).toContain(t('systemBatches.duration'))
    const finishedDuration = wrapper.get('[data-test="batch-duration-b-finished"]')
    expect(finishedDuration.text()).toBe('12.35')
    expect(finishedDuration.get('.fraction').text()).toBe('.35')
    expect(wrapper.get('[data-test="batch-duration-b-running"]').text()).toBe('1.5')
    expect(wrapper.get('[data-test="batch-duration-b-paused"]').text()).toBe('—')
    wrapper.unmount()
  })

  it('explains that only recent batches are kept when the list is empty', async () => {
    vi.mocked(systemBatches.list).mockResolvedValue(page([]))
    const wrapper = mount(SystemBatchesPanel, { global: { plugins } })
    await flushPromises()
    expect(wrapper.text()).toContain(t('systemBatches.empty'))
    expect(t('systemBatches.empty')).toContain('24')
    wrapper.unmount()
  })

  it('only allows stopping running or paused batches that are not already stopping', async () => {
    vi.mocked(systemBatches.list).mockResolvedValue(page([base, paused, stopping, finished, { ...base, id: 'b-awaiting', phase: 'awaiting', pending: 10, ok: 0, failed: 0 }]))
    const wrapper = mount(SystemBatchesPanel, { global: { plugins } })
    await flushPromises()
    const disabled = (id: string) => wrapper.get(`[data-test="stop-batch-${id}"]`).attributes('disabled') !== undefined
    expect(disabled('b-running')).toBe(false)
    expect(disabled('b-paused')).toBe(false)
    expect(disabled('b-stopping')).toBe(true)
    expect(disabled('b-finished')).toBe(true)
    expect(disabled('b-awaiting')).toBe(true)
    wrapper.unmount()
  })

  it('stops a batch after confirmation and shows the returned state', async () => {
    vi.mocked(systemBatches.list).mockResolvedValue(page([base]))
    vi.mocked(systemBatches.stop).mockResolvedValue({ ...base, stopping: true })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel').mockResolvedValueOnce('confirm' as never)
    const wrapper = mount(SystemBatchesPanel, { global: { plugins } })
    await flushPromises()
    await wrapper.get('[data-test="stop-batch-b-running"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(systemBatches.stop).not.toHaveBeenCalled()
    await wrapper.get('[data-test="stop-batch-b-running"]').trigger('click'); await flushPromises()
    expect(systemBatches.stop).toHaveBeenCalledWith('b-running')
    expect(wrapper.get('[data-test="batch-status-b-running"]').text()).toBe(t('systemBatches.stopping'))
    expect(wrapper.get('[data-test="stop-batch-b-running"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('reloads the list when the stop is refused', async () => {
    vi.mocked(systemBatches.list).mockResolvedValueOnce(page([base])).mockResolvedValue(page([finished]))
    vi.mocked(systemBatches.stop).mockRejectedValue(new Error('conflict'))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const wrapper = mount(SystemBatchesPanel, { global: { plugins } })
    await flushPromises()
    await wrapper.get('[data-test="stop-batch-b-running"]').trigger('click'); await flushPromises()
    expect(systemBatches.list).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="batch-status-b-finished"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('refreshes every 5 seconds while a batch is live and stops on unmount', async () => {
    vi.useFakeTimers()
    vi.mocked(systemBatches.list).mockResolvedValue(page([base]))
    const wrapper = mount(SystemBatchesPanel, { global: { plugins } })
    await flushPromises()
    expect(systemBatches.list).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(4999)
    expect(systemBatches.list).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(systemBatches.list).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(5000)
    expect(systemBatches.list).toHaveBeenCalledTimes(3)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(20000)
    expect(systemBatches.list).toHaveBeenCalledTimes(3)
  })

  it('does not offer stopping to someone who may only watch', async () => {
    vi.mocked(systemBatches.list).mockResolvedValue(page([{ ...base, can_stop: false }]))
    const wrapper = mount(SystemBatchesPanel, { global: { plugins } })
    await flushPromises()
    expect(wrapper.get('[data-test="stop-batch-b-running"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('opens each item with what was sent and how it went, filtered and paged', async () => {
    const item: SystemBatchItem = {
      seq: 2, target: 'SKU-2 stock', path_params: { id: 'SKU-2' }, query: null, body: { quantity: 3 }, before: { quantity: 1 },
      status: 'http_error', status_code: 409, brief: 'conflict', started_at: '2026-10-01T00:01:59.766Z', finished_at: '2026-10-01T00:02:00Z',
    }
    const waiting: SystemBatchItem = { ...item, seq: 3, status: 'pending', status_code: null, brief: null, started_at: null, finished_at: null }
    vi.mocked(systemBatches.list).mockResolvedValue(page([base]))
    vi.mocked(systemBatches.items).mockResolvedValue({ items: [item, waiting], total: 120, page: 1, per_page: 50 })
    const wrapper = mount(SystemBatchesPanel, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="batch-items-b-running"]').trigger('click'); await flushPromises()
    expect(systemBatches.items).toHaveBeenCalledWith('b-running', 1, 50, undefined)
    const drawer = document.querySelector('[data-test="batch-items-table"]')!.textContent!
    expect(drawer).toContain('SKU-2 stock')
    expect(drawer).toContain('"quantity": 3')
    expect(drawer).toContain('conflict')
    expect(document.querySelector('[data-test="item-status-2"]')!.textContent).toContain(`${t('systemBatches.itemStatus.http_error')} · 409`)
    expect(document.querySelector('[data-test="item-duration-2"]')!.textContent).toBe('0.23')
    expect(document.querySelector('[data-test="item-duration-3"]')!.textContent).toBe('—')
    const failed = [...document.querySelectorAll<HTMLElement>('[data-test="batch-items-filter"] label')].find(el => el.textContent?.includes(t('systemBatches.itemFilter.failed')))!
    failed.click(); await flushPromises()
    expect(systemBatches.items).toHaveBeenLastCalledWith('b-running', 1, 50, 'failed')
    wrapper.unmount()
  })

  it('stops polling while its tab is hidden and refreshes when shown again', async () => {
    vi.useFakeTimers()
    vi.mocked(systemBatches.list).mockResolvedValue(page([base]))
    const wrapper = mount(SystemBatchesPanel, { global: { plugins }, props: { active: true } })
    await flushPromises()
    await wrapper.setProps({ active: false })
    await vi.advanceTimersByTimeAsync(20000)
    expect(systemBatches.list).toHaveBeenCalledTimes(1)
    await wrapper.setProps({ active: true }); await flushPromises()
    expect(systemBatches.list).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(5000)
    expect(systemBatches.list).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('does not poll when every batch has ended, and refreshes on demand', async () => {
    vi.useFakeTimers()
    vi.mocked(systemBatches.list).mockResolvedValue(page([finished]))
    const wrapper = mount(SystemBatchesPanel, { global: { plugins } })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(20000)
    expect(systemBatches.list).toHaveBeenCalledTimes(1)
    await wrapper.get('[data-test="refresh-batches"]').trigger('click'); await flushPromises()
    expect(systemBatches.list).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
