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
  it('lets admins only disable a member’s own personal task', async () => {
    vi.mocked(cron.list).mockResolvedValueOnce({ items: [{ ...row, personal: true, prompt: '', can_edit: false, running_task_id: 9 }], total: 1 } as never)
    const wrapper = mount(CronView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.get('[data-test="personal-j1"]').text()).toBe(i18n.global.t('cron.personal'))
    for (const action of ['history', 'run', 'test-notification', 'edit', 'delete']) {
      expect(wrapper.find(`[data-test="${action}-j1"]`).exists(), action).toBe(false)
    }
    expect(wrapper.text()).not.toContain(i18n.global.t('cron.cancelRun'))
    await wrapper.get('[data-test="disable-j1"]').trigger('click'); await flushPromises()
    expect(cron.disable).toHaveBeenCalledWith(expect.objectContaining({ id: 'j1', version: 3 }))
    wrapper.unmount()
  })
  it('does not tag ordinary tasks as personal', async () => {
    const wrapper = mount(CronView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="personal-j1"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="delete-j1"]').exists()).toBe(true)
    wrapper.unmount()
  })
  it('shows a placeholder for private runs the viewer did not execute', async () => {
    const base = { task_id: 1, status: 'success', error_message: null, precheck_meta: null, delivery: {}, deliveries: [], started_at: '2026-09-18T01:00:00Z', finished_at: null, trigger_kind: 'scheduled', input_tokens: null, output_tokens: null }
    vi.mocked(cron.runs).mockResolvedValueOnce({ items: [
      { ...base, id: 1, private: true, prompt: '', reply: null, executed_by: 'u2' },
      { ...base, id: 2, private: true, prompt: 'my own prompt', reply: 'my own reply', executed_by: 'u1' },
    ], total: 2 } as never)
    const wrapper = mount(CronView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="history-j1"]').trigger('click'); await flushPromises()
    for (const button of document.querySelectorAll<HTMLElement>('.el-drawer .el-table__expand-icon')) button.click()
    await flushPromises()
    const drawer = document.querySelector('.el-drawer')!
    expect(drawer.querySelectorAll('[data-test="private-run"]')).toHaveLength(1)
    expect(drawer.textContent).toContain(i18n.global.t('cron.privateRun'))
    expect(drawer.textContent).toContain('my own reply')
    wrapper.unmount()
  })

})
