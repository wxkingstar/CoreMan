import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/collaboration', () => ({ collaboration: { get: vi.fn(), set: vi.fn() } }))
import { collaboration, type BotHelpSettings } from '@/api/collaboration'
import BotCollaborators from '@/components/BotCollaborators.vue'
import { i18n } from '@/i18n'
const on: BotHelpSettings = { accept_bot_help: true, permission: 'granted', active_count: 0 }
const mountPanel = (props: Partial<{ botId: string; canEdit: boolean; active: boolean }> = {}) => mount(BotCollaborators, { props: { botId: 'b1', canEdit: true, active: true, ...props }, global: { plugins: [ElementPlus, i18n] } })
const flip = (w: ReturnType<typeof mountPanel>, value: boolean) => w.getComponent({ name: 'ElSwitch' }).vm.$emit('update:modelValue', value)
beforeEach(() => { vi.clearAllMocks(); vi.mocked(collaboration.get).mockResolvedValue({ ...on }) })
afterEach(() => vi.restoreAllMocks())
it('loads only when visible and editable', async () => {
 const w = mountPanel({ active: false }); await flushPromises(); expect(collaboration.get).not.toHaveBeenCalled()
 await w.setProps({ active: true, canEdit: false }); await flushPromises(); expect(collaboration.get).not.toHaveBeenCalled(); expect(w.find('[data-test="accept-switch"]').exists()).toBe(false)
 await w.setProps({ canEdit: true }); await flushPromises(); expect(collaboration.get).toHaveBeenCalledOnce(); expect(w.text()).toContain('允许其他 AI 员工求助'); w.unmount()
})
it('turns on without confirmation', async () => {
 vi.mocked(collaboration.get).mockResolvedValue({ ...on, accept_bot_help: false }); vi.mocked(collaboration.set).mockResolvedValue({ ...on })
 const confirm = vi.spyOn(ElMessageBox, 'confirm'); const w = mountPanel(); await flushPromises()
 flip(w, true); await flushPromises(); expect(confirm).not.toHaveBeenCalled(); expect(collaboration.set).toHaveBeenCalledExactlyOnceWith('b1', true); w.unmount()
})
it('confirms before turning off and keeps state when declined', async () => {
 vi.mocked(collaboration.get).mockResolvedValue({ ...on, active_count: 2 })
 const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel'); const w = mountPanel(); await flushPromises()
 flip(w, false); await flushPromises(); expect(confirm.mock.calls[0][0]).toContain('2'); expect(collaboration.set).not.toHaveBeenCalled()
 confirm.mockResolvedValueOnce('confirm' as never); vi.mocked(collaboration.set).mockResolvedValue({ ...on, accept_bot_help: false })
 flip(w, false); await flushPromises(); expect(collaboration.set).toHaveBeenCalledExactlyOnceWith('b1', false)
 expect(w.getComponent({ name: 'ElSwitch' }).props('modelValue')).toBe(false); w.unmount()
})
it('warns when the Feishu app lacks the permissions', async () => {
 vi.mocked(collaboration.get).mockResolvedValue({ ...on, permission: 'missing' }); const w = mountPanel(); await flushPromises()
 expect(w.get('[data-test="permission"]').text()).toContain('查询群成员'); w.unmount()
})
it('retries load failures and discards stale responses', async () => {
 vi.mocked(collaboration.get).mockRejectedValueOnce(new Error('Network unavailable')); const w = mountPanel(); await flushPromises()
 expect(w.text()).toContain('Network unavailable'); await w.get('[data-test="retry"]').trigger('click'); await flushPromises(); expect(collaboration.get).toHaveBeenCalledTimes(2)
 let resolve!: (v: typeof on) => void; vi.mocked(collaboration.get).mockImplementationOnce(() => new Promise(r => { resolve = r }))
 await w.setProps({ botId: 'b2' }); await w.setProps({ botId: 'b3' }); await flushPromises(); resolve({ ...on, accept_bot_help: false }); await flushPromises()
 expect(w.getComponent({ name: 'ElSwitch' }).props('modelValue')).toBe(true); w.unmount()
})
