import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/personalCredentials', () => ({ personalCredentials: { items: vi.fn(), bots: vi.fn(), updateItem: vi.fn(), setBots: vi.fn(), removeItem: vi.fn(), rename: vi.fn() } }))
import { personalCredentials, type CredentialItem } from '@/api/personalCredentials'
import MyCredentialsView from '@/views/MyCredentialsView.vue'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'

const at = '2026-10-03T01:00:00Z'
const rows: CredentialItem[] = [
  { id: 'c1', env_key: 'DEMO_USERNAME', label: '账号', secret: false, value: 'alice', updated_at: at, last_used_at: null, bindings: [{ bot_id: 'b1', bot_name: 'Demo 助手', env_key: 'DEMO_USERNAME', last_used_at: null }] },
  { id: 'c2', env_key: 'DEMO_PIN', label: 'PIN', secret: true, value: null, updated_at: at, last_used_at: at, bindings: [{ bot_id: 'b1', bot_name: 'Demo 助手', env_key: 'DEMO_PIN', last_used_at: at }] },
  { id: 'c3', env_key: 'DEMO_PIN', label: '旧 PIN', secret: true, value: null, updated_at: at, last_used_at: null, bindings: [{ bot_id: 'b2', bot_name: '运营', env_key: 'DEMO_PIN', last_used_at: null }] },
]
const bots = [{ id: 'b1', name: 'Demo 助手', platform: 'feishu' }, { id: 'b2', name: '运营', platform: 'feishu' }]
const render = () => mount(MyCredentialsView, { global: { plugins: [ElementPlus, i18n] } })
type Select = { vm: { $emit: (event: string, value: string[]) => void } }
const select = (wrapper: ReturnType<typeof render>, id: string) =>
  wrapper.findAllComponents({ name: 'ElSelect' }).find(c => c.attributes('data-test') === `bots-${id}`) as unknown as Select

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(personalCredentials.items).mockReset().mockResolvedValue(rows)
  vi.mocked(personalCredentials.bots).mockReset().mockResolvedValue(bots)
  vi.mocked(personalCredentials.updateItem).mockReset().mockResolvedValue(rows[1])
  vi.mocked(personalCredentials.setBots).mockReset().mockResolvedValue(rows[1])
  vi.mocked(personalCredentials.removeItem).mockReset().mockResolvedValue(null)
  vi.mocked(personalCredentials.rename).mockReset().mockResolvedValue(rows[1])
})

it('is a personal page every member can open', () => {
  expect(MENU.find(item => item.path === '/my-credentials')?.roles).toBeUndefined()
})

it('lists credentials with the AI employees using them, hiding secret values', async () => {
  const wrapper = render(); await flushPromises()
  expect(wrapper.text()).toContain('Demo 助手')
  expect(wrapper.text()).toContain('alice')
  expect(wrapper.text()).toContain('已设置（不显示）')
  wrapper.unmount()
})

it('binds a credential to more AI employees, warning before replacing one of the same name', async () => {
  const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const wrapper = render(); await flushPromises()
  select(wrapper, 'c2').vm.$emit('update:modelValue', ['b1', 'b2']); await flushPromises()
  expect(String(confirm.mock.calls[0][0])).toContain('旧 PIN')
  expect(personalCredentials.setBots).toHaveBeenCalledWith('c2', ['b1', 'b2'])
  wrapper.unmount()
})

it('does not bind when the replacement is declined', async () => {
  vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
  const wrapper = render(); await flushPromises()
  select(wrapper, 'c2').vm.$emit('update:modelValue', ['b1', 'b2']); await flushPromises()
  expect(personalCredentials.setBots).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('updates a secret field through a password prompt', async () => {
  const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: 'pin-222333', action: 'confirm' } as never)
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="update-c2"]').trigger('click'); await flushPromises()
  expect(prompt.mock.calls[0][2]).toMatchObject({ inputType: 'password' })
  expect(personalCredentials.updateItem).toHaveBeenCalledWith('c2', 'pin-222333')
  wrapper.unmount()
})

it('deletes after confirmation', async () => {
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="delete-c2"]').trigger('click'); await flushPromises()
  expect(personalCredentials.removeItem).toHaveBeenCalledWith('c2')
  wrapper.unmount()
})

it('updates only that row after binding, keeping the select usable and clearing the search', async () => {
  const updated = { ...rows[0], bindings: [...rows[0].bindings, { bot_id: 'b2', bot_name: '运营', env_key: 'DEMO_USERNAME', last_used_at: null }] }
  vi.mocked(personalCredentials.setBots).mockResolvedValue(updated)
  const wrapper = render(); await flushPromises()
  const picker = wrapper.findAllComponents({ name: 'ElSelect' }).find(c => c.attributes('data-test') === 'bots-c1')!
  expect(picker.props('reserveKeyword')).toBe(false)
  ;(picker as unknown as Select).vm.$emit('update:modelValue', ['b1', 'b2']); await flushPromises()
  expect(personalCredentials.setBots).toHaveBeenCalledWith('c1', ['b1', 'b2'])
  // 没有替换别的凭证：不重新拉整页，也不禁用多选框。
  expect(personalCredentials.items).toHaveBeenCalledTimes(1)
  expect(picker.props('disabled')).toBe(false)
  expect(picker.props('modelValue')).toEqual(['b1', 'b2'])
  wrapper.unmount()
})

it('renames a credential', async () => {
  const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: '  GitHub 个人令牌 ', action: 'confirm' } as never)
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="rename-c2"]').trigger('click'); await flushPromises()
  expect(prompt.mock.calls[0][2]).toMatchObject({ inputValue: 'PIN' })
  expect(personalCredentials.rename).toHaveBeenCalledWith('c2', 'GitHub 个人令牌')
  wrapper.unmount()
})
