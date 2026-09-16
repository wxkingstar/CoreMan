import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/collaboration', () => ({ collaboration: { list: vi.fn(), options: vi.fn(), groups: vi.fn(), create: vi.fn(), verify: vi.fn(), update: vi.fn(), remove: vi.fn() } }))
import { collaboration } from '@/api/collaboration'
import BotCollaborators from '@/components/BotCollaborators.vue'
import { i18n } from '@/i18n'
const row = { id: 'r1', target_bot_id: 'b2', target_name: 'Partner', target_description: '', chat_id: 'g1', chat_name: 'Team', version: 1, enabled: false, status: 'pending', reason: null, can_enable: false, can_verify: false, can_remove: true, active_count: 0 } as const
const mountPanel = (props = {}) => mount(BotCollaborators, { props: { botId: 'b1', sourceName: 'Source', canEdit: true, active: true, ...props }, global: { plugins: [ElementPlus, i18n] } })
beforeEach(() => { vi.clearAllMocks(); vi.mocked(collaboration.list).mockResolvedValue([]); vi.mocked(collaboration.options).mockResolvedValue([{ id: 'b2', name: 'Partner', description: '', enabled: true, available: true }]); vi.mocked(collaboration.groups).mockResolvedValue([{ chat_id: 'g1', name: 'Team' }]) })
afterEach(() => vi.useRealTimers())
it('does not load hidden or unauthorized configuration', async () => {
 const wrapper = mountPanel({ active: false }); await flushPromises(); expect(collaboration.list).not.toHaveBeenCalled()
 await wrapper.setProps({ active: true, canEdit: false }); await flushPromises(); expect(collaboration.list).not.toHaveBeenCalled(); expect(wrapper.find('[data-test="add-partner"]').exists()).toBe(false); wrapper.unmount()
})
it('polls pending routes only while active and stops on unmount', async () => {
 vi.useFakeTimers(); vi.mocked(collaboration.list).mockResolvedValue([row]); const wrapper = mountPanel(); await flushPromises(); expect(collaboration.list).toHaveBeenCalledTimes(1)
 await vi.advanceTimersByTimeAsync(3000); await flushPromises(); expect(collaboration.list).toHaveBeenCalledTimes(2)
 await wrapper.setProps({ active: false }); await vi.advanceTimersByTimeAsync(6000); expect(collaboration.list).toHaveBeenCalledTimes(2); wrapper.unmount()
})
it('discards a response from the previous employee', async () => {
 let resolve!: (value: typeof row[]) => void
 vi.mocked(collaboration.list).mockImplementationOnce(() => new Promise(r => { resolve = r }))
 const wrapper = mountPanel(); await wrapper.setProps({ botId: 'b3' }); await flushPromises(); resolve([row]); await flushPromises(); expect(wrapper.text()).not.toContain('Partner'); wrapper.unmount()
})
it('shows list errors with an explicit retry', async () => {
 vi.mocked(collaboration.list).mockRejectedValueOnce(new Error('Network unavailable')); const wrapper = mountPanel(); await flushPromises(); expect(wrapper.text()).toContain('Network unavailable'); await wrapper.get('[data-test="retry-list"]').trigger('click'); await flushPromises(); expect(collaboration.list).toHaveBeenCalledTimes(2); wrapper.unmount()
})
it('creates once and waits for verification before enabling', async () => {
 const wrapper = mountPanel(); await flushPromises(); await wrapper.get('[data-test="add-partner"]').trigger('click'); await flushPromises()
 const selects = wrapper.findAllComponents({ name: 'ElSelect' })
 selects[0].vm.$emit('update:modelValue', 'b2'); selects[0].vm.$emit('change', 'b2'); await flushPromises()
 selects[1].vm.$emit('update:modelValue', 'g1'); await flushPromises()
 let complete!: (r: typeof row) => void
 vi.mocked(collaboration.create).mockImplementationOnce(() => new Promise(r => { complete = r }))
 await wrapper.get('[data-test="verify-partner"]').trigger('click'); await wrapper.get('[data-test="verify-partner"]').trigger('click')
 expect(collaboration.create).toHaveBeenCalledTimes(1); complete(row); await flushPromises()
 expect(wrapper.get('[data-test="verify-partner"]').attributes('disabled')).toBeDefined(); expect(collaboration.update).not.toHaveBeenCalled(); wrapper.unmount()
})
it('retains the form when creation fails', async () => {
 const wrapper = mountPanel(); await flushPromises(); await wrapper.get('[data-test="add-partner"]').trigger('click'); await flushPromises()
 const selects = wrapper.findAllComponents({ name: 'ElSelect' }); selects[0].vm.$emit('update:modelValue', 'b2'); selects[0].vm.$emit('change', 'b2'); await flushPromises(); selects[1].vm.$emit('update:modelValue', 'g1'); await flushPromises()
 vi.mocked(collaboration.create).mockRejectedValueOnce(new Error('Cannot verify now')); await wrapper.get('[data-test="verify-partner"]').trigger('click'); await flushPromises()
 expect(wrapper.text()).toContain('Cannot verify now'); expect(selects[0].props('modelValue')).toBe('b2'); expect(selects[1].props('modelValue')).toBe('g1'); wrapper.unmount()
})
it('ignores old group responses when the selected partner changes', async () => {
 let complete!: (groups: { chat_id: string; name: string }[]) => void
 vi.mocked(collaboration.groups).mockImplementationOnce(() => new Promise(r => { complete = r })).mockResolvedValueOnce([{ chat_id: 'new', name: 'New group' }])
 const wrapper = mountPanel(); await flushPromises(); await wrapper.get('[data-test="add-partner"]').trigger('click'); await flushPromises()
 const selects = wrapper.findAllComponents({ name: 'ElSelect' }); selects[0].vm.$emit('update:modelValue', 'b2'); selects[0].vm.$emit('change', 'b2'); await flushPromises()
 selects[0].vm.$emit('update:modelValue', 'b3'); selects[0].vm.$emit('change', 'b3'); await flushPromises()
 complete([{ chat_id: 'old', name: 'Old group' }]); await flushPromises()
 const labels = wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')); expect(labels).not.toContain('Old group'); expect(labels).toContain('New group'); wrapper.unmount()
})
it('shows actionable localized backend reasons instead of raw reason codes', async () => {
 vi.mocked(collaboration.list).mockResolvedValue([{ ...row, status: 'failed', reason: 'probe_delivery_failed', can_verify: true }])
 const wrapper = mountPanel(); await flushPromises()
 expect(wrapper.text()).toContain('测试消息发送失败'); expect(wrapper.text()).not.toContain('probe_delivery_failed'); wrapper.unmount()
})
it('shows unavailable rather than enabled when a configured route loses its runtime', async () => {
 vi.mocked(collaboration.list).mockResolvedValue([{ ...row, enabled: true, status: 'unavailable', reason: 'runtime_unavailable' }])
 const wrapper = mountPanel(); await flushPromises()
 expect(wrapper.findComponent({ name: 'ElTag' }).text()).toBe('暂不可用')
 expect(wrapper.findComponent({ name: 'ElTag' }).props('type')).toBe('info')
 expect(wrapper.text()).toContain('暂停协作'); wrapper.unmount()
})
it('ignores a late list failure after creation and continues polling the new route', async () => {
 vi.useFakeTimers()
 let rejectOld!: (error: Error) => void
 vi.mocked(collaboration.list).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject })).mockResolvedValue([row])
 const wrapper = mountPanel(); await flushPromises()
 await wrapper.get('[data-test="add-partner"]').trigger('click'); await flushPromises()
 const selects = wrapper.findAllComponents({ name: 'ElSelect' }); selects[0].vm.$emit('update:modelValue', 'b2'); selects[0].vm.$emit('change', 'b2'); await flushPromises(); selects[1].vm.$emit('update:modelValue', 'g1'); await flushPromises()
 vi.mocked(collaboration.create).mockResolvedValueOnce(row)
 await wrapper.get('[data-test="verify-partner"]').trigger('click'); await flushPromises()
 rejectOld(new Error('Old list failed')); await flushPromises()
 expect(wrapper.text()).not.toContain('Old list failed')
 await vi.advanceTimersByTimeAsync(3000); await flushPromises()
 expect(collaboration.list).toHaveBeenCalledTimes(2)
 expect(wrapper.text()).not.toContain('正在加载…'); wrapper.unmount()
})
