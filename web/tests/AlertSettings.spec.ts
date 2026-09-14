import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { expect, it, vi } from 'vitest'
vi.mock('@/api/alerts', () => ({ alerts: {
  get: vi.fn().mockResolvedValue({ version: 3, channels: [{ platform_app_id: 'app1', user_id: 'u1' }] }),
  save: vi.fn().mockResolvedValue({ version: 4, channels: [] }),
} }))
vi.mock('@/api/admin', () => ({
  platformApps: { list: vi.fn().mockResolvedValue({ items: [{ id: 'app1', name: '通知', platform: 'feishu', enabled: true, capabilities: ['notify'] }], total: 1 }) },
  users: { list: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}))
import AlertSettings from '@/components/AlertSettings.vue'
import { alerts } from '@/api/alerts'
import { i18n } from '@/i18n'
it('removes an alert channel using the loaded version', async () => {
  const wrapper = mount(AlertSettings, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  const remove = wrapper.findAll('button').find((button) => button.text() === i18n.global.t('common.delete'))!
  await remove.trigger('click')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(alerts.save).toHaveBeenCalledWith([], 3)
})
