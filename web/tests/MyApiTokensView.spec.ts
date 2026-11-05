import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/apiAccess', () => ({ apiTokens: { list: vi.fn(), bots: vi.fn(), create: vi.fn(), revoke: vi.fn() } }))
import { apiTokens, type ApiToken } from '@/api/apiAccess'
import MyApiTokensView from '@/views/MyApiTokensView.vue'
import { i18n } from '@/i18n'
import { MENU } from '@/layouts/AdminLayout.vue'

const token: ApiToken = {
  id: 't1', name: '工单系统', prefix: 'cmt_abcdefgh', status: 'active',
  bots: [{ id: 'b1', name: 'Demo 助手', bot_key: 'demo', api_access: false }],
  expires_at: '2027-01-01T00:00:00Z', last_used_at: null, revoked_at: null, created_at: '2026-11-06T00:00:00Z',
}
const render = () => mount(MyApiTokensView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(apiTokens.list).mockReset().mockResolvedValue([token])
  vi.mocked(apiTokens.bots).mockReset().mockResolvedValue(token.bots)
  vi.mocked(apiTokens.create).mockReset().mockResolvedValue({ ...token, token: 'cmt_secret-plaintext' })
  vi.mocked(apiTokens.revoke).mockReset().mockResolvedValue({ ...token, status: 'revoked' })
})

it('is a personal page every member can open', () => {
  expect(MENU.find(item => item.path === '/my-api-tokens')?.roles).toBeUndefined()
})

it('lists tokens by prefix and flags employees without API driving', async () => {
  const wrapper = render(); await flushPromises()
  expect(wrapper.text()).toContain('工单系统')
  expect(wrapper.text()).toContain('cmt_abcdefgh')
  expect(wrapper.text()).not.toContain('cmt_secret-plaintext')
  wrapper.unmount()
})

it('creates a token and shows the plaintext once', async () => {
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="create-token"]').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as { form: { name: string; botIds: string[]; days: number }; create: () => Promise<void> }
  vm.form.name = '工单系统'; vm.form.botIds = ['b1']; vm.form.days = 30
  await vm.create(); await flushPromises()
  expect(apiTokens.create).toHaveBeenCalledWith('工单系统', ['b1'], 30)
  expect(document.body.textContent).toContain('cmt_secret-plaintext')
  wrapper.unmount()
})

it('revokes after confirmation', async () => {
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const wrapper = render(); await flushPromises()
  await wrapper.get('[data-test="revoke-t1"]').trigger('click'); await flushPromises()
  expect(apiTokens.revoke).toHaveBeenCalledWith('t1')
  wrapper.unmount()
})
