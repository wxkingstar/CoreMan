import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import ElementPlus from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/skills', () => ({ allSkills: vi.fn(), skills: { installed: vi.fn(), install: vi.fn(), uninstall: vi.fn(), sources: vi.fn(), presets: vi.fn(), save: vi.fn() } }))
import { allSkills, skills } from '@/api/skills'
import SkillEditorDialog from '@/components/skills/SkillEditorDialog.vue'
import BotSkills from '@/components/BotSkills.vue'
import { i18n } from '@/i18n'

beforeEach(() => vi.clearAllMocks())
it('loads only on open and submits a scope without marking the skill installed', async () => {
  const item = { id: 's1', name: 'query', description: 'Query', enabled: true, security_level: 'internal', selectable_env_groups: { erp: 'ERP' }, user_env_vars: {}, security_prompt_template: 'Read only', default_data_source: null }
  vi.mocked(allSkills).mockResolvedValue([item] as never)
  vi.mocked(skills.installed).mockResolvedValue({ items: [], pending_approvals: [] })
  vi.mocked(skills.install).mockResolvedValue({ status: 'pending_approval' })
  const wrapper = mount(BotSkills, { props: { botId: 'b1' }, global: { plugins: [createPinia(), ElementPlus, i18n], stubs: { teleport: true } } })
  await flushPromises()
  expect(skills.installed).not.toHaveBeenCalled()
  await wrapper.get('[data-test="skills-open"]').trigger('click'); await flushPromises()
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
  await wrapper.get('[data-test="skills-open"]').trigger('click'); await flushPromises()
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
