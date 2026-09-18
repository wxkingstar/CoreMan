import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
vi.mock('@/api/selfReminders', () => ({ selfReminders: { list: vi.fn(), cancel: vi.fn(), pause: vi.fn(), resume: vi.fn(), remove: vi.fn() } }))
import { ApiError } from '@/api/client'
import { selfReminders, type SelfReminder, type SelfSchedule } from '@/api/selfReminders'
import SelfRemindersView from '@/views/SelfRemindersView.vue'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'

const reminder: SelfReminder = { id: 'r1', type: 'reminder', bot_name: 'B', text: '检查接口', run_at: '2026-09-17T04:00:00Z', enabled: true, status: 'scheduled', deliveries: [], can_cancel: true }
const schedule: SelfSchedule = {
  id: 's1', type: 'schedule', bot_name: 'Demo Bot', enabled: true, name: '未读消息日报',
  text: '总结我昨天以来的飞书未读消息', schedule_kind: 'recurring', cron_expression: '0 9 * * 1-5', timezone: 'Asia/Shanghai',
  run_at: null, next_run_at: '2026-09-21T01:00:00Z', status: 'failed', running: false,
  last_run: { status: 'failed', started_at: '2026-09-18T01:00:00Z', finished_at: '2026-09-18T01:01:00Z', reply: '<b>not bold</b>\nsecond line', truncated: true, error: 'model timeout', deliveries: ['sent'] },
  can_pause: true, can_resume: false,
}
const render = () => mount(SelfRemindersView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(selfReminders.list).mockReset().mockResolvedValue([reminder, schedule])
  vi.mocked(selfReminders.cancel).mockReset().mockResolvedValue(reminder)
  vi.mocked(selfReminders.pause).mockReset().mockResolvedValue({ ...schedule, enabled: false })
  vi.mocked(selfReminders.resume).mockReset().mockResolvedValue(schedule)
  vi.mocked(selfReminders.remove).mockReset().mockResolvedValue(null)
})

it('ordinary owners can find, inspect and cancel a fixed reminder without AI editor controls', async () => {
  expect(MENU.find(item => item.path === '/self-reminders')?.roles).toBeUndefined()
  expect(MENU.some(item => item.path === '/self-reminders')).toBe(true)
  const wrapper = render()
  await flushPromises()
  expect(wrapper.text()).toContain('检查接口')
  expect(wrapper.text()).toContain('2026')
  expect(wrapper.find('textarea').exists()).toBe(false)
  await wrapper.get('[data-test="cancel-r1"]').trigger('click')
  await flushPromises()
  expect(selfReminders.cancel).toHaveBeenCalledWith('r1')
  wrapper.unmount()
})

describe('own scheduled AI tasks', () => {
  it('is titled as scheduled tasks and explains both reminders and AI tasks', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.get('h1').text()).toBe('我的定时任务')
    expect(wrapper.text()).toContain('确认提醒')
    expect(wrapper.text()).toContain('确认创建')
    expect(wrapper.text()).toContain('10')
    wrapper.unmount()
  })

  it('shows schedule, next run, status, instruction and last result as plain text', async () => {
    const wrapper = render(); await flushPromises()
    const card = wrapper.get('[data-test="schedule-s1"]')
    expect(card.text()).toContain('未读消息日报')
    expect(card.text()).toContain('Demo Bot')
    expect(card.get('code').text()).toBe('0 9 * * 1-5')
    expect(card.text()).toContain('Asia/Shanghai')
    expect(card.text()).toContain('2026')
    expect(card.text()).toContain(i18n.global.t('selfReminders.scheduleStatus.failed'))
    expect(card.get('details.instruction').attributes('open')).toBeUndefined()
    expect(card.get('details.instruction').text()).toContain('总结我昨天以来的飞书未读消息')
    // 结果按纯文本渲染：HTML 标签原样显示，不会变成元素。
    expect(card.get('pre.reply').text()).toContain('<b>not bold</b>')
    expect(card.find('pre.reply b').exists()).toBe(false)
    expect(card.get('.run-error').text()).toContain('model timeout')
    expect(card.text()).toContain(i18n.global.t('selfReminders.truncated'))
    expect(card.text()).toContain(i18n.global.t('selfReminders.status.sent'))
    expect(card.find('[data-test="pause-s1"]').exists()).toBe(true)
    expect(card.find('[data-test="resume-s1"]').exists()).toBe(false)
    expect(card.find('[data-test="cancel-s1"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows a one-off task that has not run yet', async () => {
    vi.mocked(selfReminders.list).mockResolvedValue([{ ...schedule, schedule_kind: 'once', cron_expression: '', run_at: '2026-09-20T02:00:00Z', status: 'scheduled', last_run: null }])
    const wrapper = render(); await flushPromises()
    const card = wrapper.get('[data-test="schedule-s1"]')
    expect(card.text()).toContain(i18n.global.t('selfReminders.once'))
    expect(card.find('code').exists()).toBe(false)
    expect(card.text()).toContain(i18n.global.t('selfReminders.noRun'))
    wrapper.unmount()
  })

  it('pauses and reloads', async () => {
    const wrapper = render(); await flushPromises()
    vi.mocked(selfReminders.list).mockResolvedValue([reminder, { ...schedule, enabled: false, can_pause: false, can_resume: true }])
    await wrapper.get('[data-test="pause-s1"]').trigger('click'); await flushPromises()
    expect(selfReminders.pause).toHaveBeenCalledWith('s1')
    expect(wrapper.find('[data-test="pause-s1"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="resume-s1"]').exists()).toBe(true)
    expect(wrapper.get('[data-test="schedule-s1"]').text()).toContain(i18n.global.t('selfReminders.state.paused'))
    wrapper.unmount()
  })

  it('shows the server message when resuming is refused', async () => {
    vi.mocked(selfReminders.list).mockResolvedValue([{ ...schedule, enabled: false, can_pause: false, can_resume: true }])
    vi.mocked(selfReminders.resume).mockRejectedValue(new ApiError(409, 409, '每人最多保留 10 个启用中的定时任务，请先暂停或删除'))
    const error = vi.spyOn(ElMessage, 'error')
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="resume-s1"]').trigger('click'); await flushPromises()
    expect(selfReminders.resume).toHaveBeenCalledWith('s1')
    expect(error).toHaveBeenCalledWith('每人最多保留 10 个启用中的定时任务，请先暂停或删除')
    expect(wrapper.find('[data-test="resume-s1"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('explains a paused one-off task that can no longer resume', async () => {
    vi.mocked(selfReminders.list).mockResolvedValue([{ ...schedule, schedule_kind: 'once', cron_expression: '', run_at: '2026-09-01T02:00:00Z', enabled: false, can_pause: false, can_resume: false }])
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="resume-s1"]').exists()).toBe(false)
    expect(wrapper.text()).toContain(i18n.global.t('selfReminders.expiredOnce'))
    wrapper.unmount()
  })

  it('deletes only after confirmation', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
    const error = vi.spyOn(ElMessage, 'error')
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="delete-s1"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalledWith(i18n.global.t('selfReminders.confirmDelete', { name: schedule.name }), i18n.global.t('selfReminders.delete'), { type: 'warning' })
    expect(selfReminders.remove).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    confirm.mockResolvedValueOnce('confirm' as never)
    vi.mocked(selfReminders.list).mockResolvedValue([reminder])
    await wrapper.get('[data-test="delete-s1"]').trigger('click'); await flushPromises()
    expect(selfReminders.remove).toHaveBeenCalledWith('s1')
    expect(wrapper.find('[data-test="schedule-s1"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
