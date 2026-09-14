import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
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
