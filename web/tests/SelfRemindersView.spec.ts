import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { expect, it, vi } from 'vitest'
vi.mock('@/api/selfReminders', () => ({ selfReminders: { list: vi.fn().mockResolvedValue([{ id:'r1', bot_name:'B', text:'检查接口', run_at:'2026-09-17T04:00:00Z', enabled:true, status:'scheduled', deliveries:[], can_cancel:true }]), cancel: vi.fn().mockResolvedValue({}) } }))
import { selfReminders } from '@/api/selfReminders'
import SelfRemindersView from '@/views/SelfRemindersView.vue'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'
it('ordinary owners can find, inspect and cancel a fixed reminder without AI editor controls', async () => {
  expect(MENU.find(item => item.path === '/self-reminders')?.roles).toBeUndefined()
  expect(MENU.some(item => item.path === '/self-reminders')).toBe(true)
  const wrapper = mount(SelfRemindersView, { global: { plugins:[ElementPlus, i18n] } })
  await flushPromises()
  expect(wrapper.text()).toContain('检查接口')
  expect(wrapper.text()).toContain('2026')
  expect(wrapper.find('textarea').exists()).toBe(false)
  await wrapper.get('[data-test="cancel-r1"]').trigger('click')
  await flushPromises()
  expect(selfReminders.cancel).toHaveBeenCalledWith('r1')
})
