import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/personalCredentials', () => ({ personalCredentials: { list: vi.fn(), update: vi.fn(), remove: vi.fn(), request: vi.fn(), submit: vi.fn() } }))
import { personalCredentials, type PersonalCredential } from '@/api/personalCredentials'
import MyCredentialsView from '@/views/MyCredentialsView.vue'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'

const rows: PersonalCredential[] = [
  { bot_id: 'b1', bot_name: 'Demo 助手', env_key: 'DEMO_USERNAME', label: '账号', secret: false, value: 'alice', updated_at: '2026-10-03T01:00:00Z', last_used_at: null },
  { bot_id: 'b1', bot_name: 'Demo 助手', env_key: 'DEMO_PIN', label: 'PIN', secret: true, value: null, updated_at: '2026-10-03T01:00:00Z', last_used_at: '2026-10-03T02:00:00Z' },
]
const render = () => mount(MyCredentialsView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(personalCredentials.list).mockReset().mockResolvedValue(rows)
  vi.mocked(personalCredentials.update).mockReset().mockResolvedValue(rows[1])
  vi.mocked(personalCredentials.remove).mockReset().mockResolvedValue(null)
})

it('is a personal page every member can open', () => {
  expect(MENU.find(item => item.path === '/my-credentials')?.roles).toBeUndefined()
})

it('groups by AI employee, shows plain fields and hides secret ones', async () => {
  const wrapper = render(); await flushPromises()
  expect(wrapper.text()).toContain('Demo 助手')
  expect(wrapper.text()).toContain('alice')
  expect(wrapper.text()).toContain('已设置（不显示）')
  wrapper.unmount()
})

it('updates a secret field through a password prompt', async () => {
  const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: 'pin-222333', action: 'confirm' } as never)
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="update-DEMO_PIN"]').trigger('click'); await flushPromises()
  expect(prompt.mock.calls[0][2]).toMatchObject({ inputType: 'password' })
  expect(personalCredentials.update).toHaveBeenCalledWith('b1', 'DEMO_PIN', 'pin-222333')
  wrapper.unmount()
})

it('deletes after confirmation', async () => {
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="delete-DEMO_PIN"]').trigger('click'); await flushPromises()
  expect(personalCredentials.remove).toHaveBeenCalledWith('b1', 'DEMO_PIN')
  wrapper.unmount()
})
