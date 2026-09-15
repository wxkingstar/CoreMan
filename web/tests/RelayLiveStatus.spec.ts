import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { expect, it, vi } from 'vitest'
import { i18n } from '@/i18n'
import RelayLiveStatus from '@/components/RelayLiveStatus.vue'
import { http } from '@/api/client'

vi.mock('@/api/client', () => ({
  http: { get: vi.fn(), post: vi.fn() },
  call: (promise: Promise<unknown>) => promise,
  ApiError: class ApiError extends Error {},
}))

it('loads only on opening and clears stale tasks when refresh fails', async () => {
  vi.mocked(http.get).mockResolvedValue({ active_tasks: [{ pid: 1, bot_key: 'bot1', user: 'human', chat_id: 'chat1' }] })
  const wrapper = mount(RelayLiveStatus, { props: { relayId: 'relay1', name: 'Relay' }, global: { plugins: [ElementPlus, i18n] } })
  expect(http.get).not.toHaveBeenCalled()
  await wrapper.get('button').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as { tasks: unknown[]; error: string; refresh: () => Promise<void> }
  expect(vm.tasks).toHaveLength(1)
  vi.mocked(http.get).mockRejectedValue(new Error('Agent unavailable'))
  await vm.refresh()
  expect(vm.tasks).toHaveLength(0)
  expect(vm.error).toBe('Agent unavailable')
  wrapper.unmount()
})
