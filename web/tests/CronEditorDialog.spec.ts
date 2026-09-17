import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElDatePicker, ElMessageBox, ElRadioGroup } from 'element-plus'
import { expect, it, vi } from 'vitest'
import { i18n } from '@/i18n'
import CronEditorDialog from '@/components/cron/CronEditorDialog.vue'
import { cron } from '@/api/cron'
vi.mock('@/api/cron', () => ({ cron: { notificationChats: vi.fn().mockResolvedValue([{ id: 'group', name: '销售群' }]), create: vi.fn().mockResolvedValue({}), update: vi.fn(), precheck: vi.fn() } }))
vi.mock('@/api/admin', () => ({ users: { get: vi.fn().mockResolvedValue({ id: 'u1', display_name: '王鑫' }) } }))
it('saves a once instant in UTC only after showing timezone and recipient confirmation', async () => {
  const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const wrapper = mount(CronEditorDialog, { props: { botOptions: [{ id: 'b1', name: '员工', platform: 'feishu' }] as never }, global: { plugins: [ElementPlus, i18n] } })
  await wrapper.vm.open({ id: 'j1', version: 3, bot_id: 'b1', name: '提醒', prompt: 'check', target_users: ['u1'], target_chats: [], notify_emails: [], schedule_kind: 'recurring', run_at: null, cron_expression: '0 9 * * *', timezone: 'Asia/Shanghai', enabled: true } as never)
  await flushPromises()
  const kind = wrapper.findComponent(ElRadioGroup)
  expect(kind.exists()).toBe(true)
  kind.vm.$emit('update:modelValue', 'once')
  await flushPromises()
  const future = new Date('2030-01-01T01:30:00.000Z')
  wrapper.findAllComponents(ElDatePicker)[0]!.vm.$emit('update:modelValue', future)
  await flushPromises()
  await wrapper.get('[data-test="save-cron"]').trigger('click')
  await flushPromises()
  expect(confirm.mock.calls[0]?.[0]).toContain('仅执行一次')
  expect(confirm.mock.calls[0]?.[0]).toContain('王鑫')
  expect(confirm.mock.calls[0]?.[0]).toContain(Intl.DateTimeFormat().resolvedOptions().timeZone)
  expect(cron.update).toHaveBeenCalledWith('j1', expect.objectContaining({ schedule_kind: 'once', run_at: future.toISOString() }), 3)
  wrapper.unmount()
})

it.each([false, true])('preserves saved timezone on metadata edits, changes it only with execution time (%s)', async (changeTime) => {
  vi.clearAllMocks()
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone
  const savedZone = browserZone === 'UTC' ? 'Asia/Tokyo' : 'UTC'
  const savedTime = '2030-01-01T01:30:00.000Z'
  const editedTime = '2030-01-02T01:30:00.000Z'
  const wrapper = mount(CronEditorDialog, { props: { botOptions: [{ id: 'b1', name: '员工', platform: 'feishu' }] as never }, global: { plugins: [ElementPlus, i18n] } })
  await wrapper.vm.open({ id: 'j1', version: 3, bot_id: 'b1', name: '提醒', prompt: 'check', target_users: ['u1'], target_chats: [], notify_emails: [], schedule_kind: 'once', run_at: savedTime, cron_expression: '', timezone: savedZone, enabled: true, running_task_id: changeTime ? null : 10 } as never)
  await flushPromises()
  await wrapper.get('input.el-input__inner').setValue('新名称')
  if (changeTime) {
    wrapper.findAllComponents(ElDatePicker)[0]!.vm.$emit('update:modelValue', new Date(editedTime))
    await flushPromises()
  }
  await wrapper.get('[data-test="save-cron"]').trigger('click')
  await flushPromises()
  expect(cron.update).toHaveBeenCalledWith('j1', expect.objectContaining({
    name: '新名称', schedule_kind: 'once', run_at: changeTime ? editedTime : savedTime,
    timezone: changeTime ? browserZone : savedZone,
  }), 3)
  wrapper.unmount()
})
