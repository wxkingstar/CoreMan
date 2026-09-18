import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import RuntimeNodesView from '@/views/RuntimeNodesView.vue'
import { runtimeNodes } from '@/api/runtimeNodes'

it('submits a changed project root and explains acknowledgement', async () => {
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="edit-runtime"]').trigger('click'); await flushPromises()
  expect(wrapper.find('[data-test="runtime-root"]').exists()).toBe(true)
  const vm = wrapper.vm as unknown as { editForm: { workspace_root: string }; saveEdit: () => Promise<void> }
  vm.editForm.workspace_root = '/home/ai/new'
  await vm.saveEdit()
  expect(runtimeNodes.patch).toHaveBeenLastCalledWith('node1', { workspace_root: '/home/ai/new' })
  wrapper.unmount()
})
vi.mock('@/api/runtimeNodes', () => ({ runtimeNodes: {
  list: vi.fn().mockResolvedValue([{ id: 'node1', name: 'AI Server', hostname: 'host1', username: 'ai', platform: 'linux', architecture: 'arm64', environment: 'host', workspace_root: '/work', online: true, is_active: true, draining: false,
    team_id: null, team_name: null, root_edit_supported: true, capabilities: { claude: { installed: true, version: 'v1', login: 'ready' }, codex: { installed: true, version: 'v2', login: 'required' } }, backends: [] }]),
  links: vi.fn().mockResolvedValue([]), createLink: vi.fn().mockResolvedValue({ id: 'link1', command: 'curl -fsSL https://example.test/install | sh', expires_at: '2026-09-14T00:00:00Z' }), patch: vi.fn(), remove: vi.fn(), revokeLink: vi.fn(),
} }))
vi.mock('@/api/admin', () => ({ relays: { probe: vi.fn() }, teams: { list: vi.fn().mockResolvedValue([{ id: 'team1', slug: 'dev', name_zh: '研发', name_ja: null, name_en: null }]) }, catalog: { list: vi.fn().mockResolvedValue([]) } }))
type EditVm = { editForm: { name: string; team_id: string | null }; editDialog: boolean; saveEdit: () => Promise<void> }
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
  expect(wrapper.find('[data-test="edit-runtime"]').exists()).toBe(false)
  expect(wrapper.find('[data-test="delete-runtime"]').exists()).toBe(false)
  wrapper.unmount()
})
it('edits a runtime name and rejects a blank name', async () => {
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="edit-runtime"]').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as EditVm
  expect(vm.editForm.name).toBe('AI Server')
  vm.editForm.name = '   '
  await vm.saveEdit()
  expect(runtimeNodes.patch).not.toHaveBeenCalled()
  vm.editForm.name = '  研发 Mac  '
  await vm.saveEdit()
  expect(runtimeNodes.patch).toHaveBeenCalledWith('node1', { name: '研发 Mac' })
  expect(vm.editDialog).toBe(false)
  wrapper.unmount()
})
// 团队决定谁能把 AI 员工建到这台机器上：只提交真正改动的字段，清空即改回公共池。
it('moves a runtime between a team and the shared pool', async () => {
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="edit-runtime"]').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as EditVm
  expect(vm.editForm.team_id).toBe(null)
  await vm.saveEdit()  // 什么都没改：不发请求，直接关闭。
  expect(runtimeNodes.patch).not.toHaveBeenCalled()
  expect(vm.editDialog).toBe(false)
  vm.editForm.team_id = 'team1'
  await vm.saveEdit(); await flushPromises()
  expect(runtimeNodes.patch).toHaveBeenCalledWith('node1', { team_id: 'team1' })
  vi.mocked(runtimeNodes.list).mockResolvedValueOnce([{ id: 'node1', name: 'AI Server', team_id: 'team1', team_name: '研发', capabilities: {}, backends: [] }] as never)
  await (wrapper.vm as unknown as { refresh: (silent?: boolean) => Promise<void> }).refresh(); await flushPromises()
  await wrapper.get('[data-test="edit-runtime"]').trigger('click'); await flushPromises()
  expect(vm.editForm.team_id).toBe('team1')
  vm.editForm.team_id = ''  // el-select 清空
  await vm.saveEdit()
  expect(runtimeNodes.patch).toHaveBeenLastCalledWith('node1', { team_id: null })
  wrapper.unmount()
})
it('keeps the entered name when saving fails', async () => {
  vi.mocked(runtimeNodes.patch).mockRejectedValueOnce(new Error('保存失败'))
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="edit-runtime"]').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as EditVm
  vm.editForm.name = '研发 Mac'
  await vm.saveEdit()
  expect(vm.editDialog).toBe(true)
  expect(vm.editForm.name).toBe('研发 Mac')
  wrapper.unmount()
})
// 删除不可恢复，且会让主机上的 Daemon 失效：绑定中的员工先拦下来，其余必须确认。
it('blocks deleting a runtime that still has bots and confirms otherwise', async () => {
  const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
  const backend = { id: 'relay1', model_provider: 'claude', bot_count: 2, effective_models: [], health_status: 'healthy' }
  const busy = { id: 'node1', name: 'AI Server', team_id: null, team_name: null, online: true, is_active: true, draining: false, capabilities: {}, backends: [backend] }
  vi.mocked(runtimeNodes.list).mockResolvedValueOnce([busy] as never)
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="delete-runtime"]').trigger('click'); await flushPromises()
  expect(confirm).not.toHaveBeenCalled()
  expect(runtimeNodes.remove).not.toHaveBeenCalled()
  vi.mocked(runtimeNodes.list).mockResolvedValueOnce([{ ...busy, backends: [{ ...backend, bot_count: 0 }] }] as never)
  await (wrapper.vm as unknown as { refresh: (silent?: boolean) => Promise<void> }).refresh(); await flushPromises()
  await wrapper.get('[data-test="delete-runtime"]').trigger('click'); await flushPromises()
  expect(String(confirm.mock.calls[0][0])).toContain('「AI Server」')
  expect(runtimeNodes.remove).not.toHaveBeenCalled()
  confirm.mockResolvedValueOnce('confirm' as never)
  await wrapper.get('[data-test="delete-runtime"]').trigger('click'); await flushPromises()
  expect(runtimeNodes.remove).toHaveBeenCalledWith('node1')
  confirm.mockRestore()
  wrapper.unmount()
})
it('requires the project root before creating a one-time install command', async () => {
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="install-runtime"]').trigger('click'); await flushPromises()
  const vm = wrapper.vm as unknown as { form: { workspace_root: string }; create: () => Promise<void>; generated: { command: string } | null }
  expect(vm.form.workspace_root).toBe('/home/ai')
  vm.form.workspace_root = ''
  await vm.create()
  expect(runtimeNodes.createLink).not.toHaveBeenCalled()
  vm.form.workspace_root = '/home/ai/projects'
  await vm.create(); await flushPromises()
  expect(runtimeNodes.createLink).toHaveBeenCalledWith(expect.objectContaining({ workspace_root: '/home/ai/projects' }))
  expect(vm.generated?.command).toContain('curl -fsSL')
  expect(runtimeNodes.createLink).toHaveBeenCalledWith(expect.objectContaining({ options: expect.objectContaining({ max_concurrent: 10, git_hosts: ['github.com'] }) }))
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
// 内网自签 / 私有 CA 的 HTTPS 地址需要随安装链接下发 CA 证书；留空不传，填了要即时校验格式与大小。
it('validates the optional private CA certificate and only sends it when filled', async () => {
  const wrapper = mountPage('platform_admin'); await flushPromises()
  await wrapper.get('[data-test="install-runtime"]').trigger('click'); await flushPromises()
  expect(wrapper.find('[data-test="runtime-ca-pem"]').exists()).toBe(true)
  const vm = wrapper.vm as unknown as { form: { workspace_root: string; options: { ca_pem: string } }; caPemError: string; create: () => Promise<void>; generated: unknown }
  vm.form.workspace_root = '/home/ai/projects'
  await vm.create(); await flushPromises()
  expect(vi.mocked(runtimeNodes.createLink).mock.calls[0][0].options).not.toHaveProperty('ca_pem')
  vm.form.options.ca_pem = 'not a certificate'
  expect(vm.caPemError).toBe(i18n.global.t('runtimeNodes.caPemInvalid'))
  await vm.create()
  expect(runtimeNodes.createLink).toHaveBeenCalledTimes(1)
  vm.form.options.ca_pem = `-----BEGIN CERTIFICATE-----\n${'A'.repeat(65 * 1024)}\n-----END CERTIFICATE-----`
  expect(vm.caPemError).toBe(i18n.global.t('runtimeNodes.caPemTooLarge'))
  const pem = '-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ\n-----END CERTIFICATE-----\n'
  vm.form.options.ca_pem = pem
  expect(vm.caPemError).toBe('')
  await vm.create(); await flushPromises()
  expect(vi.mocked(runtimeNodes.createLink).mock.calls[1][0].options).toMatchObject({ ca_pem: pem.trim(), max_concurrent: 10 })
  wrapper.unmount()
})
// 节点白名单只能在节点 config.json 修改；管理台只读展示，方便解释「Git 来源不在白名单内」。
it('shows the reported Git host allowlist and flags entries that are not host names', async () => {
  const base = { hostname: 'host1', username: 'ai', platform: 'linux', architecture: 'arm64', environment: 'host', workspace_root: '/work', online: true, is_active: true, draining: false, capabilities: {}, backends: [] }
  vi.mocked(runtimeNodes.list).mockResolvedValueOnce([
    { ...base, id: 'n1', name: 'reported', git_hosts: ['github.com', 'https://git.corp.example/'] },
    { ...base, id: 'n2', name: 'legacy', git_hosts: null },
    { ...base, id: 'n3', name: 'empty', git_hosts: [] },
  ] as never)
  const wrapper = mountPage('member'); await flushPromises()
  for (const icon of wrapper.findAll('.el-table__expand-icon')) await icon.trigger('click')
  await flushPromises()
  const rows = wrapper.findAll('[data-test="runtime-git-hosts"]')
  expect(rows).toHaveLength(3)
  const tags = rows[0].findAll('.el-tag')
  expect(tags.map(tag => tag.text())).toEqual(['github.com', 'https://git.corp.example/'])
  expect(tags[0].classes()).toContain('el-tag--info')
  expect(tags[1].classes()).toContain('el-tag--danger')
  expect(rows[0].text()).toContain('以下条目不是主机名，不会匹配任何仓库：https://git.corp.example/')
  expect(rows[1].text()).toContain('未上报（节点版本较旧）')
  expect(rows[2].text()).toContain('空（不允许任何 Git 主机）')
  expect(rows[1].text()).not.toContain('不是主机名')
  wrapper.unmount()
})
