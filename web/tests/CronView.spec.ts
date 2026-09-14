import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
const { row } = vi.hoisted(() => ({ row: {
  id: 'j1', bot_id: 'b1', name: '日报', cron_expression: '0 9 * * *', timezone: 'Asia/Shanghai', prompt: 'report',
  system_prompt: null, precheck_script: null, precheck_timeout_seconds: 30, enabled: true, expires_at: null,
  target_users: [], target_chats: [], notify_emails: [], notify_webhook: false, has_webhook_url: false, version: 3, created_by: 'u1',
  can_edit: true, next_run_at: '2026-09-14T01:00:00Z', force_run_at: null, running_task_id: null, last_status: null,
} }))
vi.mock('@/api/cron', () => ({ cron: {
  list: vi.fn().mockResolvedValue({ items: [row], total: 1 }), create: vi.fn().mockResolvedValue(row),
  update: vi.fn().mockResolvedValue(row), run: vi.fn().mockResolvedValue(row), disable: vi.fn(), remove: vi.fn(),
  notificationChats: vi.fn().mockResolvedValue(['chat-1']),
  testNotification: vi.fn().mockResolvedValue({ outbox_ids: [1], errors: {} }),
  notificationTests: vi.fn().mockResolvedValue([{ id: 1, platform: 'wecom', channel: 'webhook', status: 'pending', attempts: 0, error: null }]),
  runs: vi.fn().mockResolvedValue({ items: [], total: 0 }), precheck: vi.fn(),
} }))
vi.mock('@/api/admin', () => ({ bots: { list: vi.fn().mockResolvedValue({ items: [{ id: 'b1', name: '销售' }] }) }, users: { get: vi.fn() } }))
import { cron } from '@/api/cron'
import { i18n } from '@/i18n'
import CronView from '@/views/CronView.vue'
describe('CronView', () => {
  beforeEach(() => vi.clearAllMocks())
  it('manual run passes the current version and history loads on demand', async () => {
    const wrapper = mount(CronView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(cron.runs).not.toHaveBeenCalled()
    await wrapper.get('[data-test="run-j1"]').trigger('click'); await flushPromises()
    expect(cron.run).toHaveBeenCalledWith(expect.objectContaining({ id: 'j1', version: 3 }))
    await wrapper.get('[data-test="history-j1"]').trigger('click'); await flushPromises()
    expect(cron.runs).toHaveBeenCalledWith('j1', 1)
    wrapper.unmount()
  })
  it('cannot edit another creator task or immediately run an active one', async () => {
    vi.mocked(cron.list).mockResolvedValueOnce({ items: [{ ...row, can_edit: false, running_task_id: 9 }], total: 1 } as never)
    const wrapper = mount(CronView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="edit-j1"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="run-j1"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
  it('sends a notification test without running the AI task and shows delivery status', async () => {
    const wrapper = mount(CronView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    await wrapper.get('[data-test="test-notification-j1"]').trigger('click'); await flushPromises()
    expect(cron.testNotification).toHaveBeenCalledWith(expect.objectContaining({ id: 'j1', version: 3 }))
    expect(cron.run).not.toHaveBeenCalled()
    expect(cron.notificationTests).toHaveBeenCalledWith('j1')
    expect(wrapper.text()).toContain('待发送')
    wrapper.unmount()
  })

})
