import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import ElementPlus from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/skills', () => ({ allSkills: vi.fn(), skills: { installed: vi.fn(), install: vi.fn(), uninstall: vi.fn(), sources: vi.fn(), presets: vi.fn(), save: vi.fn(), mcpLoginStart: vi.fn(), mcpLoginComplete: vi.fn(), mcpLogout: vi.fn() } }))
import { allSkills, skills } from '@/api/skills'
import SkillEditorDialog from '@/components/skills/SkillEditorDialog.vue'
import BotSkills from '@/components/BotSkills.vue'
import { i18n } from '@/i18n'

beforeEach(() => vi.clearAllMocks())
it('loads on mount and submits a scope without marking the skill installed', async () => {
  const item = { id: 's1', name: 'query', description: 'Query', enabled: true, security_level: 'internal', selectable_env_groups: { erp: 'ERP' }, user_env_vars: {}, security_prompt_template: 'Read only', default_data_source: null }
  vi.mocked(allSkills).mockResolvedValue([item] as never)
  vi.mocked(skills.installed).mockResolvedValue({ items: [], pending_approvals: [] })
  vi.mocked(skills.install).mockResolvedValue({ status: 'pending_approval' })
  const wrapper = mount(BotSkills, { props: { botId: 'b1' }, global: { plugins: [createPinia(), ElementPlus, i18n], stubs: { teleport: true } } })
  await flushPromises()
  expect(skills.installed).toHaveBeenCalledWith('b1')
  await wrapper.get('[data-test="skill-install-s1"]').trigger('click'); await flushPromises()
  await wrapper.get('[data-test="skill-submit"]').trigger('click'); await flushPromises()
  expect(skills.install).toHaveBeenCalledWith('b1', 's1', expect.objectContaining({ selected_env_groups: [], requested_security_prompt: 'Read only' }))
  expect(wrapper.text()).not.toContain('已安装')
  wrapper.unmount()
})

it('shows disabled catalog entries and lets managers configure them before installation', async () => {
  const item = { source_id: 'source', env_groups: [], data_sources: null, default_data_source: null, doris_enabled_groups: [], install_type: 'git', external_repo_url: null, security_prompt_template: null, category: null, version: null, id: 's2', name: 'disabled-query', description: 'Query', enabled: false, security_level: 'public', selectable_env_groups: {}, user_env_vars: {} }
  vi.mocked(allSkills).mockResolvedValue([item] as never)
  vi.mocked(skills.installed).mockResolvedValue({ items: [], pending_approvals: [] })
  const pinia = createPinia()
  useAuthStore(pinia).user = { role: 'platform_admin' } as never
  const wrapper = mount(BotSkills, { props: { botId: 'b1' }, attachTo: document.body, global: { plugins: [pinia, ElementPlus, i18n], stubs: { teleport: true, ElSelect: true } } })
  await flushPromises()
  expect(wrapper.text()).toContain('disabled-query')
  expect(wrapper.text()).toContain('已禁用')
  expect(wrapper.find('[data-test="skill-configure-s2"]').exists()).toBe(true)
  expect(wrapper.find('[data-test="skill-install-s2"]').exists()).toBe(false)
  expect(skills.install).not.toHaveBeenCalled()
  vi.mocked(skills.sources).mockResolvedValue([{ id: 'source', key: 'tools', label: 'Tools', categories: {} }] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  await wrapper.get('[data-test="skill-configure-s2"]').trigger('click'); await flushPromises()
  const editor = wrapper.findComponent(SkillEditorDialog).vm
  expect(editor.form.name).toBe('disabled-query')
  expect(editor.form.enabled).toBe(false)
  editor.form.source_id = 'source'
  editor.form.enabled = true
  vi.mocked(allSkills).mockResolvedValue([{ ...item, enabled: true }] as never)
  await editor.save(); await flushPromises()
  expect(skills.save).toHaveBeenCalledWith(expect.objectContaining({ id: 's2' }), expect.objectContaining({ enabled: true }))
  expect(wrapper.find('[data-test="skill-install-s2"]').exists()).toBe(true)
  wrapper.unmount()
})

it('keeps descriptions and install errors on a single truncated line', async () => {
  const item = { id: 's1', name: 'query', description: 'Long description', enabled: true, security_level: 'public', selectable_env_groups: {}, user_env_vars: {}, security_prompt_template: null, default_data_source: null }
  vi.mocked(allSkills).mockResolvedValue([item] as never)
  vi.mocked(skills.installed).mockResolvedValue({ items: [{ skill_id: 's1', name: 'query', status: 'failed', revision: 1, version: '1.0', selected_env_groups: [], user_env_vars: {}, security_prompt: null, error_message: 'git clone failed', mcp_oauth: false, mcp_authorized: false, mcp_authorized_at: null, mcp_authorized_by: null }], pending_approvals: [] })
  const wrapper = mount(BotSkills, { props: { botId: 'b1' }, global: { plugins: [createPinia(), ElementPlus, i18n], stubs: { teleport: true } } })
  await flushPromises()
  expect(wrapper.findAll('.truncated-text-preview').map(node => node.text())).toEqual(['安装未完成 · git clone failed', 'Long description'])
  expect(wrapper.find('.el-table p').exists()).toBe(false)
  wrapper.unmount()
})

it('authorizes an OAuth MCP skill through the runtime and shows who authorized it', async () => {
  const installed = { skill_id: 's3', name: 'figma', status: 'installed', revision: 1, version: '1.0', selected_env_groups: [], user_env_vars: {}, security_prompt: null, error_message: null, mcp_oauth: true, mcp_authorized: false, mcp_authorized_at: null, mcp_authorized_by: null }
  vi.mocked(allSkills).mockResolvedValue([])
  vi.mocked(skills.installed).mockResolvedValue({ items: [installed], pending_approvals: [] })
  vi.mocked(skills.mcpLoginStart).mockResolvedValue({ session_id: 'session-1', authorization_url: 'https://auth.example.com/oauth?state=x', expires_in: 600 })
  vi.mocked(skills.mcpLoginComplete).mockResolvedValue(undefined as never)
  const wrapper = mount(BotSkills, { props: { botId: 'b1' }, global: { plugins: [createPinia(), ElementPlus, i18n], stubs: { teleport: true } } })
  await flushPromises()
  expect(wrapper.get('[data-test="mcp-state"]').text()).toBe('未授权')
  expect(wrapper.find('[data-test="mcp-revoke"]').exists()).toBe(false)
  await wrapper.get('[data-test="mcp-authorize"]').trigger('click'); await flushPromises()
  expect(skills.mcpLoginStart).toHaveBeenCalledWith('b1', 's3')
  expect(wrapper.get('[data-test="mcp-open"]').attributes('href')).toBe('https://auth.example.com/oauth?state=x')
  vi.mocked(skills.installed).mockResolvedValue({ items: [{ ...installed, mcp_authorized: true, mcp_authorized_at: '2026-10-12T00:00:00Z', mcp_authorized_by: '王小明' }], pending_approvals: [] })
  await wrapper.get('textarea[data-test="mcp-callback"]').setValue(' http://127.0.0.1:5000/callback?code=abc&state=x ')
  await wrapper.get('[data-test="mcp-complete"]').trigger('click'); await flushPromises()
  expect(skills.mcpLoginComplete).toHaveBeenCalledWith('b1', 's3', { session_id: 'session-1', callback_url: 'http://127.0.0.1:5000/callback?code=abc&state=x' })
  expect(wrapper.get('[data-test="mcp-state"]').text()).toBe('已由 王小明 授权')
  expect(wrapper.find('[data-test="mcp-revoke"]').exists()).toBe(true)
  wrapper.unmount()
})

it('asks to authorize again after the employee moved to another runtime', async () => {
  vi.mocked(allSkills).mockResolvedValue([])
  vi.mocked(skills.installed).mockResolvedValue({ items: [{ skill_id: 's3', name: 'figma', status: 'installed', revision: 1, version: null, selected_env_groups: [], user_env_vars: {}, security_prompt: null, error_message: null, mcp_oauth: true, mcp_authorized: false, mcp_authorized_at: '2026-10-12T00:00:00Z', mcp_authorized_by: '王小明' }], pending_approvals: [] })
  const wrapper = mount(BotSkills, { props: { botId: 'b1' }, global: { plugins: [createPinia(), ElementPlus, i18n], stubs: { teleport: true } } })
  await flushPromises()
  expect(wrapper.get('[data-test="mcp-state"]').text()).toBe('需重新授权（员工换了运行时）')
  expect(wrapper.get('[data-test="mcp-authorize"]').text()).toBe('授权')
  wrapper.unmount()
})
