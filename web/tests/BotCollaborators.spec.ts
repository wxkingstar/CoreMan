import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/collaboration', () => ({ collaboration: { list: vi.fn(), options: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() } }))
import { collaboration } from '@/api/collaboration'
import BotCollaborators from '@/components/BotCollaborators.vue'
import { i18n } from '@/i18n'
const row = { id: 'r1', target_bot_id: 'b2', target_name: 'Partner', target_description: '', version: 1, enabled: true, status: 'ready', reason: null, can_enable: true, can_remove: true, active_count: 0 } as const
const peer = { id: 'b2', name: 'Partner', description: '', enabled: true, available: true }
const mountPanel = (props = {}) => mount(BotCollaborators, { props: { botId: 'b1', sourceName: 'Source', canEdit: true, active: true, ...props }, global: { plugins: [ElementPlus, i18n] } })
async function openPanel() { const w = mountPanel(); await flushPromises(); await w.get('[data-test="add-partner"]').trigger('click'); await flushPromises(); return w }
async function select(w: ReturnType<typeof mountPanel>) { w.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'b2'); await flushPromises() }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(collaboration.list).mockResolvedValue([]); vi.mocked(collaboration.options).mockResolvedValue([peer]) })
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })
it('guards hidden and unauthorized sources', async () => {
 const w = mountPanel({ active: false }); await flushPromises(); expect(collaboration.list).not.toHaveBeenCalled()
 await w.setProps({ active: true, canEdit: false }); await flushPromises(); expect(collaboration.list).not.toHaveBeenCalled(); expect(w.find('[data-test="add-partner"]').exists()).toBe(false); w.unmount()
})
it('does not poll partners', async () => {
 vi.useFakeTimers(); vi.mocked(collaboration.list).mockResolvedValue([row]); const w = mountPanel(); await flushPromises(); await vi.advanceTimersByTimeAsync(9000); expect(collaboration.list).toHaveBeenCalledTimes(1); w.unmount()
})
it('discards previous-source list responses', async () => {
 let resolve!: (value: typeof row[]) => void
 vi.mocked(collaboration.list).mockImplementationOnce(() => new Promise(r => { resolve = r }))
 const w = mountPanel(); await w.setProps({ botId: 'b3' }); await flushPromises(); resolve([row]); await flushPromises(); expect(w.text()).not.toContain('Partner'); w.unmount()
})
it('retries list failures', async () => {
 vi.mocked(collaboration.list).mockRejectedValueOnce(new Error('Network unavailable')); const w = mountPanel(); await flushPromises(); expect(w.text()).toContain('Network unavailable'); await w.get('[data-test="retry-list"]').trigger('click'); await flushPromises(); expect(collaboration.list).toHaveBeenCalledTimes(2); w.unmount()
})
it('saves once with only a partner, closes and confirms enabled success', async () => {
 const toast = vi.spyOn(ElMessage, 'success'); const w = await openPanel(); expect(w.findAllComponents({ name: 'ElSelect' })).toHaveLength(1)
 expect(w.get('[data-test="save-partner"]').attributes('disabled')).toBeDefined(); await select(w)
 let complete!: (r: typeof row) => void; vi.mocked(collaboration.create).mockImplementationOnce(() => new Promise(r => { complete = r }))
 await w.get('[data-test="save-partner"]').trigger('click'); await w.get('[data-test="save-partner"]').trigger('click')
 expect(collaboration.create).toHaveBeenCalledExactlyOnceWith('b1', 'b2'); complete(row); await flushPromises()
 expect(w.findComponent({ name: 'ElDrawer' }).props('modelValue')).toBe(false); expect(toast).toHaveBeenCalledWith('协作伙伴已保存并启用'); expect(collaboration.update).not.toHaveBeenCalled(); expect(w.text()).toContain('已启用'); w.unmount()
})
it('allows unavailable partners and disables only already-added options', async () => {
 vi.mocked(collaboration.options).mockResolvedValue([{ ...peer, available: false, enabled: false }, { ...peer, id: 'b4', name: 'Existing' }]); vi.mocked(collaboration.list).mockResolvedValue([{ ...row, target_bot_id: 'b4' }])
 const w = await openPanel(); const options = w.findAllComponents({ name: 'ElOption' }); expect(options[0].props('disabled')).toBe(false); expect(options[0].props('label')).toContain('暂不可用'); expect(options[1].props('disabled')).toBe(true)
 await select(w); expect(w.get('[data-test="save-partner"]').attributes('disabled')).toBeUndefined(); w.unmount()
})
it('retains selection after save failure', async () => {
 const w = await openPanel(); await select(w); vi.mocked(collaboration.create).mockRejectedValueOnce(new Error('Cannot save now')); await w.get('[data-test="save-partner"]').trigger('click'); await flushPromises()
 expect(w.text()).toContain('Cannot save now'); expect(w.findComponent({ name: 'ElSelect' }).props('modelValue')).toBe('b2'); w.unmount()
})
it('ignores obsolete search results and shows search errors', async () => {
 const w = await openPanel(); let complete!: (r: typeof peer[]) => void
 vi.mocked(collaboration.options).mockImplementationOnce(() => new Promise(r => { complete = r })).mockResolvedValueOnce([{ ...peer, id: 'new', name: 'Newest' }])
 const search = w.findComponent({ name: 'ElSelect' }).props('remoteMethod'); const old = search('old'); await search('new'); complete([{ ...peer, name: 'Old' }]); await old; await flushPromises()
 expect(w.findAllComponents({ name: 'ElOption' }).map(o => o.props('label'))).toEqual(['Newest'])
 vi.mocked(collaboration.options).mockRejectedValueOnce(new Error('Search failed')); await search('missing'); await flushPromises(); expect(w.text()).toContain('Search failed'); w.unmount()
})
it('discards a save response after source changes', async () => {
 const w = await openPanel(); await select(w); let complete!: (r: typeof row) => void; vi.mocked(collaboration.create).mockImplementationOnce(() => new Promise(r => { complete = r }))
 const toast = vi.spyOn(ElMessage, 'success'); await w.get('[data-test="save-partner"]').trigger('click'); await w.setProps({ botId: 'b3' }); complete(row); await flushPromises()
 expect(w.text()).not.toContain('Partner'); expect(toast).not.toHaveBeenCalled(); expect(w.findComponent({ name: 'ElDrawer' }).props('modelValue')).toBe(false); w.unmount()
})
it('discards options after access is revoked', async () => {
 let complete!: (r: typeof peer[]) => void; vi.mocked(collaboration.options).mockImplementationOnce(() => new Promise(r => { complete = r }))
 const w = await openPanel(); await w.setProps({ canEdit: false }); complete([peer]); await flushPromises(); expect(w.findComponent({ name: 'ElSelect' }).exists()).toBe(false); expect(collaboration.create).not.toHaveBeenCalled(); w.unmount()
})
it('shows runtime unavailability without preventing pause', async () => {
 vi.mocked(collaboration.list).mockResolvedValue([{ ...row, status: 'unavailable', reason: 'runtime_unavailable' }]); const w = mountPanel(); await flushPromises()
 expect(w.findComponent({ name: 'ElTag' }).text()).toBe('暂不可用'); expect(w.text()).toContain('运行节点暂不可用'); expect(w.text()).toContain('暂停协作'); w.unmount()
})
it('uses the latest revision to pause and remove', async () => {
 vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never); vi.mocked(collaboration.list).mockResolvedValue([row]); vi.mocked(collaboration.update).mockResolvedValue({ ...row, enabled: false, version: 2 }); vi.mocked(collaboration.remove).mockResolvedValue()
 const w = mountPanel(); await flushPromises(); await w.findAllComponents({ name: 'ElButton' }).find(b => b.text() === '暂停协作')!.trigger('click'); await flushPromises(); expect(collaboration.update).toHaveBeenCalledWith('b1', 'r1', false, 1)
 await w.findAllComponents({ name: 'ElButton' }).find(b => b.text() === '移除伙伴')!.trigger('click'); await flushPromises(); expect(collaboration.remove).toHaveBeenCalledWith('b1', 'r1', 2); expect(w.text()).not.toContain('Partner'); w.unmount()
})
it('ignores a late list failure after saving', async () => {
 let rejectOld!: (e: Error) => void; vi.mocked(collaboration.list).mockImplementationOnce(() => new Promise((_r, reject) => { rejectOld = reject }))
 const w = await openPanel(); await select(w); vi.mocked(collaboration.create).mockResolvedValueOnce(row); await w.get('[data-test="save-partner"]').trigger('click'); await flushPromises(); rejectOld(new Error('Old list failed')); await flushPromises()
 expect(w.text()).not.toContain('Old list failed'); expect(w.text()).not.toContain('正在加载…'); w.unmount()
})
