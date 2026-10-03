import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('vue-router', async (orig) => ({ ...(await orig<typeof import('vue-router')>()), useRoute: () => ({ params: { id: 'r1' } }) }))
vi.mock('@/api/personalCredentials', () => ({ personalCredentials: { request: vi.fn(), submit: vi.fn(), list: vi.fn(), update: vi.fn(), remove: vi.fn() } }))
import { personalCredentials, type CredentialRequest } from '@/api/personalCredentials'
import CredentialRequestView from '@/views/CredentialRequestView.vue'
import { i18n } from '@/i18n'

const open: CredentialRequest = {
  id: 'r1', bot_name: 'Demo 助手', platform: 'wecom', purpose: '查询你在 Demo 系统里的订单',
  fields: [
    { key: 'DEMO_USERNAME', label: '账号', secret: false, placeholder: '' },
    { key: 'DEMO_PIN', label: 'PIN', secret: true, placeholder: '' },
  ],
  status: 'open', expires_at: '2026-10-03T02:00:00Z', security_note: '🔒 安全说明：不会发送给 AI 模型。',
}
const render = () => mount(CredentialRequestView, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.mocked(personalCredentials.request).mockReset().mockResolvedValue(open)
  vi.mocked(personalCredentials.submit).mockReset().mockResolvedValue({ status: 'saved', keys: ['DEMO_PIN', 'DEMO_USERNAME'], message: '' })
})

it('shows who asks, why, a password box for secret fields and the security note', async () => {
  const wrapper = render(); await flushPromises()
  expect(wrapper.text()).toContain('Demo 助手')
  expect(wrapper.text()).toContain('查询你在 Demo 系统里的订单')
  expect(wrapper.text()).toContain('不会发送给 AI 模型')
  expect(wrapper.get('input[data-test="field-DEMO_PIN"]').attributes('type')).toBe('password')
  expect(wrapper.get('input[data-test="field-DEMO_USERNAME"]').attributes('type')).toBe('text')
  wrapper.unmount()
})

it('submits every field once and then shows the saved state', async () => {
  const wrapper = render(); await flushPromises()
  await wrapper.get('input[data-test="field-DEMO_USERNAME"]').setValue('alice')
  await wrapper.get('input[data-test="field-DEMO_PIN"]').setValue('pin-778899')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  expect(personalCredentials.submit).toHaveBeenCalledWith('r1', { DEMO_USERNAME: 'alice', DEMO_PIN: 'pin-778899' })
  expect(wrapper.find('[data-test="saved"]').exists()).toBe(true)
  expect(wrapper.html()).not.toContain('pin-778899')
  wrapper.unmount()
})

it('refuses to submit with an empty field', async () => {
  const wrapper = render(); await flushPromises()
  await wrapper.get('input[data-test="field-DEMO_USERNAME"]').setValue('alice')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  expect(personalCredentials.submit).not.toHaveBeenCalled()
  expect(wrapper.get('[data-test="error"]').text()).toContain('PIN')
  wrapper.unmount()
})

it('shows a closed form without inputs', async () => {
  vi.mocked(personalCredentials.request).mockResolvedValue({ ...open, status: 'expired' })
  const wrapper = render(); await flushPromises()
  expect(wrapper.find('[data-test="closed"]').exists()).toBe(true)
  expect(wrapper.find('form').exists()).toBe(false)
  wrapper.unmount()
})
