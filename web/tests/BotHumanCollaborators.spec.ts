import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/collaboration', () => ({ humanCollaboration: { list: vi.fn(), options: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() } }))
import { humanCollaboration } from '@/api/collaboration'
import BotHumanCollaborators from '@/components/BotHumanCollaborators.vue'
import { i18n } from '@/i18n'
const row = { id: 'h1', user_id: 'u2', name: '库存专家', position: '主管', department: '仓储部', responsibility: '负责库存口径', enabled: true, version: 1, reachable: true, active_count: 0 }
const person = { id: 'u2', name: '库存专家', login_name: 'expert', position: '主管', department: '仓储部', added: false }
const mountPanel = (props = {}) => mount(BotHumanCollaborators, { props: { botId: 'b1', canEdit: true, active: true, ...props }, global: { plugins: [ElementPlus, i18n] } })
async function openPanel() { const w = mountPanel(); await flushPromises(); await w.get('[data-test="add-human"]').trigger('click'); await flushPromises(); return w }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(humanCollaboration.list).mockResolvedValue([]); vi.mocked(humanCollaboration.options).mockResolvedValue([person]) })
afterEach(() => { vi.restoreAllMocks() })
it('stays hidden and silent without edit access', async () => {
 const w = mountPanel({ canEdit: false }); await flushPromises(); expect(humanCollaboration.list).not.toHaveBeenCalled(); expect(w.find('[data-test="add-human"]').exists()).toBe(false); w.unmount()
})
it('adds a colleague with a responsibility and lists them', async () => {
 const toast = vi.spyOn(ElMessage, 'success'); vi.mocked(humanCollaboration.create).mockResolvedValue(row)
 const w = await openPanel(); expect(w.findComponent({ name: 'ElOption' }).props('label')).toBe('库存专家（仓储部 · 主管）')
 expect(w.get('[data-test="save-human"]').attributes('disabled')).toBeDefined()
 w.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'u2'); await w.get('textarea[data-test="human-responsibility"]').setValue(' 负责库存口径 '); await flushPromises()
 await w.get('[data-test="save-human"]').trigger('click'); await flushPromises()
 expect(humanCollaboration.create).toHaveBeenCalledExactlyOnceWith('b1', 'u2', '负责库存口径'); expect(toast).toHaveBeenCalledWith('同事已添加')
 expect(w.findAll('[data-test="human-row"]')).toHaveLength(1); expect(w.text()).toContain('负责库存口径'); w.unmount()
})
it('explains when nobody can be added instead of showing an empty closed select', async () => {
 vi.mocked(humanCollaboration.options).mockResolvedValue([]); const w = await openPanel()
 expect(w.get('[data-test="no-humans"]').text()).toContain('已绑定飞书账号'); w.unmount()
})
it('marks already added people and unreachable members', async () => {
 vi.mocked(humanCollaboration.list).mockResolvedValue([{ ...row, reachable: false }]); vi.mocked(humanCollaboration.options).mockResolvedValue([{ ...person, added: true }])
 const w = await openPanel(); const option = w.findComponent({ name: 'ElOption' }); expect(option.props('disabled')).toBe(true); expect(option.props('label')).toContain('已添加')
 expect(w.text()).toContain('无法联系'); w.unmount()
})
it('edits only the responsibility with the current revision', async () => {
 vi.mocked(humanCollaboration.list).mockResolvedValue([row]); vi.mocked(humanCollaboration.update).mockResolvedValue({ ...row, responsibility: '盘点差异', version: 2 })
 const w = mountPanel(); await flushPromises(); await w.findAllComponents({ name: 'ElButton' }).find(b => b.text() === '编辑说明')!.trigger('click'); await flushPromises()
 expect(humanCollaboration.options).not.toHaveBeenCalled(); expect(w.findComponent({ name: 'ElSelect' }).props('disabled')).toBe(true)
 expect(w.get('[data-test="save-human"]').attributes('disabled')).toBeDefined()
 await w.get('textarea[data-test="human-responsibility"]').setValue('盘点差异'); await w.get('[data-test="save-human"]').trigger('click'); await flushPromises()
 expect(humanCollaboration.update).toHaveBeenCalledWith('b1', 'h1', { responsibility: '盘点差异' }, 1); expect(w.text()).toContain('盘点差异'); w.unmount()
})
it('confirms before pausing or removing and uses the latest revision', async () => {
 const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
 vi.mocked(humanCollaboration.list).mockResolvedValue([{ ...row, active_count: 2 }]); vi.mocked(humanCollaboration.update).mockResolvedValue({ ...row, enabled: false, version: 2, active_count: 0 }); vi.mocked(humanCollaboration.remove).mockResolvedValue()
 const w = mountPanel(); await flushPromises(); await w.findAllComponents({ name: 'ElButton' }).find(b => b.text() === '暂停协作')!.trigger('click'); await flushPromises()
 expect(confirm.mock.calls[0][0]).toContain('（2 项）'); expect(humanCollaboration.update).toHaveBeenCalledWith('b1', 'h1', { enabled: false }, 1); expect(w.text()).toContain('已暂停')
 await w.findAllComponents({ name: 'ElButton' }).find(b => b.text() === '移除同事')!.trigger('click'); await flushPromises()
 expect(humanCollaboration.remove).toHaveBeenCalledWith('b1', 'h1', 2); expect(w.findAll('[data-test="human-row"]')).toHaveLength(0); w.unmount()
})
it('keeps the form after a failed save', async () => {
 vi.mocked(humanCollaboration.create).mockRejectedValueOnce(new Error('Cannot save now')); const w = await openPanel()
 w.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'u2'); await flushPromises(); await w.get('[data-test="save-human"]').trigger('click'); await flushPromises()
 expect(w.text()).toContain('Cannot save now'); expect(w.findComponent({ name: 'ElSelect' }).props('modelValue')).toBe('u2'); w.unmount()
})
