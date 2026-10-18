import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  bots: { approvers: vi.fn(), setApprovers: vi.fn() },
  users: { list: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 }) },
}))

import { bots } from '@/api/admin'
import BotApproversPanel from '@/components/bot/BotApproversPanel.vue'
import UserPicker from '@/components/UserPicker.vue'
import { i18n } from '@/i18n'

const perms = (can_edit: boolean) => ({ can_edit }) as never
const mountPanel = (can_edit = true) =>
  mount(BotApproversPanel, { props: { botId: 'b1', perms: perms(can_edit) }, global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(bots.approvers).mockResolvedValue([])
})

it('shows the empty-state hint when there are no approvers', async () => {
  const w = mountPanel()
  await flushPromises()
  expect(bots.approvers).toHaveBeenCalledWith('b1')
  expect(w.text()).toContain('为空时由发起人自己确认')
})

it('lists existing approvers read-only without edit permission', async () => {
  vi.mocked(bots.approvers).mockResolvedValue([{ user_id: 'u1', login_name: 'zhangsan', display_name: '张三' }])
  const w = mountPanel(false)
  await flushPromises()
  expect(w.text()).toContain('张三')
  expect(w.findComponent(UserPicker).exists()).toBe(false)
})

it('saves the picked approvers via the replace endpoint', async () => {
  vi.mocked(bots.setApprovers).mockResolvedValue([{ user_id: 'u9', login_name: 'lisi', display_name: '李四' }])
  const w = mountPanel()
  await flushPromises()
  w.findComponent(UserPicker).vm.$emit('update:modelValue', ['u9'])
  await w.get('[data-test="save-approvers"]').trigger('click')
  await flushPromises()
  expect(bots.setApprovers).toHaveBeenCalledWith('b1', ['u9'])
})
