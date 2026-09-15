import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import RuntimeNodesView from '@/views/RuntimeNodesView.vue'
import { runtimeNodes } from '@/api/runtimeNodes'
vi.mock('@/api/runtimeNodes', () => ({ runtimeNodes: {
  list: vi.fn().mockResolvedValue([{ id: 'node1', name: 'AI Server', hostname: 'host1', username: 'ai', platform: 'linux', architecture: 'arm64', environment: 'host', workspace_root: '/work', online: true, is_active: true, draining: false,
    capabilities: { claude: { installed: true, version: 'v1', login: 'ready' }, codex: { installed: true, version: 'v2', login: 'required' } }, backends: [] }]),
  links: vi.fn().mockResolvedValue([]), createLink: vi.fn().mockResolvedValue({ id: 'link1', command: 'curl -fsSL https://example.test/install | sh', expires_at: '2026-09-14T00:00:00Z' }), patch: vi.fn(), revokeLink: vi.fn(),
} }))
vi.mock('@/api/admin', () => ({ relays: { probe: vi.fn() }, teams: { list: vi.fn().mockResolvedValue([]) }, catalog: { list: vi.fn().mockResolvedValue([]) } }))
beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks() })
function mountPage(role: string) {
  useAuthStore().user = { id: 'me', login_name: 'test', display_name: 'Test', role, locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
  return mount(RuntimeNodesView, { global: { plugins: [ElementPlus, i18n] } })
}
it('shows one runtime with separate Claude/Codex login states', async () => {
  const wrapper = mountPage('member'); await flushPromises()
  expect(wrapper.text()).toContain('AI Server')
  expect(wrapper.text()).toContain('Claude Code')
  expect(wrapper.text()).toContain('Codex / GPT')
  expect(wrapper.text()).toContain('已登录')
  expect(wrapper.text()).toContain('待登录')
  expect(wrapper.find('[data-test="install-runtime"]').exists()).toBe(false)
  expect(wrapper.find('[data-test="rename-runtime"]').exists()).toBe(false)
  wrapper.unmount()
})
it('edits a runtime name and rejects a blank name', async () => {
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="rename-runtime"]').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as { renameName: string; renameDialog: boolean; saveName: () => Promise<void> }
  expect(vm.renameName).toBe('AI Server')
  vm.renameName = '   '
  await vm.saveName()
  expect(runtimeNodes.patch).not.toHaveBeenCalled()
  vm.renameName = '  研发 Mac  '
  await vm.saveName()
  expect(runtimeNodes.patch).toHaveBeenCalledWith('node1', { name: '研发 Mac' })
  expect(vm.renameDialog).toBe(false)
  wrapper.unmount()
})
it('keeps the entered name when saving fails', async () => {
  vi.mocked(runtimeNodes.patch).mockRejectedValueOnce(new Error('保存失败'))
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="rename-runtime"]').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as { renameName: string; renameDialog: boolean; saveName: () => Promise<void> }
  vm.renameName = '研发 Mac'
  await vm.saveName()
  expect(vm.renameDialog).toBe(true)
  expect(vm.renameName).toBe('研发 Mac')
  wrapper.unmount()
})
it('requires the project root before creating a one-time install command', async () => {
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="install-runtime"]').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as { form: { workspace_root: string }; create: () => Promise<void>; generated: { command: string } | null }
  await vm.create()
  expect(runtimeNodes.createLink).not.toHaveBeenCalled()
  vm.form.workspace_root = '/home/ai/projects'
  await vm.create(); await flushPromises()
  expect(runtimeNodes.createLink).toHaveBeenCalledWith(expect.objectContaining({ workspace_root: '/home/ai/projects' }))
  expect(vm.generated?.command).toContain('curl -fsSL')
  expect(runtimeNodes.createLink).toHaveBeenCalledWith(expect.objectContaining({ options: expect.objectContaining({ max_concurrent: 10 }) }))
  expect(wrapper.text()).not.toContain('有效小时数')
  expect(wrapper.find('[id=tab-links]').exists()).toBe(false)
  wrapper.unmount()
})
// 停用会级联停掉该节点全部运行时并取消进行中的调用，误触一次代价很大。
it('asks before disabling or draining a runtime and explains the impact', async () => {
  const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="toggle-runtime"]').trigger('click'); await flushPromises()
  expect(confirm).toHaveBeenCalledTimes(1)
  // 列表 mock 每次返回同一个对象，前面的改名用例可能已经改过名字，这里取当前渲染的名字。
  const name = (wrapper.vm as unknown as { nodes: { name: string }[] }).nodes[0].name
  expect(String(confirm.mock.calls[0][0])).toContain(`「${name}」`)
  expect(String(confirm.mock.calls[0][0])).toContain('取消')
  expect(runtimeNodes.patch).not.toHaveBeenCalled()
  confirm.mockResolvedValueOnce('confirm' as never)
  await wrapper.get('[data-test="toggle-runtime"]').trigger('click'); await flushPromises()
  expect(runtimeNodes.patch).toHaveBeenCalledWith('node1', { is_active: false })
  confirm.mockRejectedValueOnce('cancel')
  await wrapper.get('[data-test="drain-runtime"]').trigger('click'); await flushPromises()
  expect(runtimeNodes.patch).toHaveBeenCalledTimes(1)
  confirm.mockResolvedValueOnce('confirm' as never)
  await wrapper.get('[data-test="drain-runtime"]').trigger('click'); await flushPromises()
  expect(runtimeNodes.patch).toHaveBeenLastCalledWith('node1', { draining: true })
  expect(confirm).toHaveBeenCalledTimes(4)
  confirm.mockRestore()
  wrapper.unmount()
})
it('re-enables and resumes a runtime without asking', async () => {
  const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const stopped = { id: 'node1', name: 'AI Server', hostname: 'host1', username: 'ai', platform: 'linux', architecture: 'arm64', environment: 'host', workspace_root: '/work', online: true, is_active: false, draining: true,
    capabilities: { claude: { installed: true, version: 'v1', login: 'ready' }, codex: { installed: true, version: 'v2', login: 'required' } }, backends: [] }
  vi.mocked(runtimeNodes.list).mockResolvedValueOnce([stopped] as never).mockResolvedValueOnce([stopped] as never)
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="toggle-runtime"]').trigger('click'); await flushPromises()
  expect(runtimeNodes.patch).toHaveBeenCalledWith('node1', { is_active: true })
  await wrapper.get('[data-test="drain-runtime"]').trigger('click'); await flushPromises()
  expect(runtimeNodes.patch).toHaveBeenLastCalledWith('node1', { draining: false })
  expect(confirm).not.toHaveBeenCalled()
  confirm.mockRestore()
  wrapper.unmount()
})
it('shows node concurrency only when the daemon reports it', async () => {
  const base = { hostname: 'host1', username: 'ai', platform: 'linux', architecture: 'arm64', environment: 'host', workspace_root: '/work', online: true, is_active: true, draining: false, capabilities: {}, backends: [] }
  vi.mocked(runtimeNodes.list).mockResolvedValueOnce([
    { ...base, id: 'n1', name: 'busy', max_concurrent: 10, active_calls: 3 },
    { ...base, id: 'n2', name: 'quiet', max_concurrent: null, active_calls: null },
  ] as never)
  const wrapper = mountPage('member'); await flushPromises()
  const cells = wrapper.findAll('[data-test="runtime-concurrency"]')
  expect(cells).toHaveLength(1)
  expect(cells[0].text()).toBe('并发 3 / 10')
  wrapper.unmount()
})
