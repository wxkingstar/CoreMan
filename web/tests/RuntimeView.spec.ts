import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  runtime: {
    instances: vi.fn().mockResolvedValue([{ id: 'worker-a:h:1:1', service: 'worker', version: 'dev', started_at: '', heartbeat_at: '2026-09-11T00:00:00Z', alive: true, capacity: 30, running: 1, drain_requested_at: null, stopped_at: null }]),
    leases: vi.fn().mockResolvedValue([{ bot_id: 'b1', bot_key: 'sales_bot', bot_name: '销售', platform: 'wecom', holder_instance: 'gateway-wecom-a:h:1:1', generation: 3, connection_state: 'subscribed', acquired_at: '', heartbeat_at: '', drain_requested_by: null, released_at: null }]),
    queue: vi.fn().mockResolvedValue({ queued: { normal: 2, fast: 0 }, claimed: 0, running: 1, outbox_pending: 0, outbox_failed: 1, streams_active: 1 }),
    tasks: vi.fn().mockResolvedValue([{ id: 5, bot_id: 'b1', bot_key: 'sales_bot', kind: 'chat', lane: 'normal', priority: 0, session_key: 'zs', status: 'running', claimed_by: 'worker-a:h:1:1', run_after: '', claimed_at: '', started_at: '', heartbeat_at: '', cancel_requested_at: null, cancel_reason: null, attempts: 1 }]),
    outbox: vi.fn().mockResolvedValue([{ id: 7, bot_id: 'b1', bot_key: 'sales_bot', kind: 'send', dedupe_key: '5:send:1', status: 'failed', attempts: 6, not_before: '', last_error: 'boom', created_at: '', sent_at: null }]),
    retryOutbox: vi.fn().mockResolvedValue(null), cancelTask: vi.fn().mockResolvedValue(null), drain: vi.fn().mockResolvedValue(null),
  },
}))

import { runtime } from '@/api/admin'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import RuntimeView from '@/views/RuntimeView.vue'

function user(role: string) {
  return { id: 'me', login_name: 'x', display_name: 'X', role, locale: 'zh', email: null, avatar_url: null, source: 'sync' as const, team_id: null }
}

describe('RuntimeView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('renders queue, instances, leases and lets platform_admin retry', async () => {
    useAuthStore().user = user('platform_admin')
    const wrapper = mount(RuntimeView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).toContain('worker-a:h:1:1')
    expect(wrapper.text()).toContain('sales_bot')
    expect(wrapper.text()).toContain(i18n.global.t('runtime.states.subscribed'))
    await wrapper.get('[data-test="retry-7"]').trigger('click')
    await flushPromises()
    expect(runtime.retryOutbox).toHaveBeenCalledWith(7)
    expect(wrapper.find('[data-test="drain-worker-a:h:1:1"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('tags a draining lease and disables its drain button only', async () => {
    useAuthStore().user = user('platform_admin')
    const lease = { bot_id: 'b1', bot_key: 'sales_bot', bot_name: '销售', platform: 'wecom', holder_instance: 'gateway-wecom-a:h:1:1', generation: 3, connection_state: 'subscribed' as const, acquired_at: '', heartbeat_at: '', released_at: null }
    vi.mocked(runtime.leases).mockResolvedValueOnce([
      { ...lease, drain_requested_by: 'admin' },
      { ...lease, bot_id: 'b2', bot_key: 'ops_bot', drain_requested_by: null },
    ])
    const wrapper = mount(RuntimeView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).toContain(i18n.global.t('runtime.draining'))
    expect(wrapper.get('[data-test="drain-bot-sales_bot"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-test="drain-bot-ops_bot"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('hides write buttons for team_lead', async () => {
    useAuthStore().user = user('team_lead')
    const wrapper = mount(RuntimeView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="retry-7"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="cancel-5"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
