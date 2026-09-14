import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { expect, it, vi } from 'vitest'
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user: { role: 'ai_committee' } }) }))
vi.mock('@/api/skills', () => ({ allSkills: vi.fn(), skills: { sources: vi.fn(), presets: vi.fn(), save: vi.fn(), presetSave: vi.fn(), sourceSync: vi.fn() } }))
import { allSkills, skills, type Skill, type SkillInput } from '@/api/skills'
import SkillsView from '@/views/SkillsView.vue'
import SkillEnvEditor from '@/components/SkillEnvEditor.vue'
import { i18n } from '@/i18n'

it('preserves encrypted MCP configuration and security fields when editing catalog metadata', async () => {
  const row = { id: 's1', revision: 4, name: 'query', source_id: 'source', description: 'old', category: null, security_level: 'internal', version: '1.0', env_groups: [], selectable_env_groups: {}, data_sources: null, default_data_source: null, doris_enabled_groups: [], user_env_vars: {}, install_type: 'mcp', external_repo_url: null, security_prompt_template: 'Read only', enabled: true, has_mcp_config: true } as Skill
  vi.mocked(allSkills).mockResolvedValue([row])
  vi.mocked(skills.sources).mockResolvedValue([{ id: 'source', key: 'tools', label: 'Tools' }] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  vi.mocked(skills.save).mockResolvedValue(row)
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
  await flushPromises()
  const vm = wrapper.vm as unknown as { edit: (row: Skill) => void; rows: Skill[]; form: SkillInput; save: () => Promise<void> }
  vm.edit(vm.rows[0]!); await flushPromises()
  vm.form.description = 'new'
  await vm.save(); await flushPromises()
  expect(skills.save).toHaveBeenCalledWith(expect.objectContaining({ revision: 4 }), expect.objectContaining({ description: 'new', mcp_config: null, security_prompt_template: 'Read only', security_level: 'internal' }))
  wrapper.unmount()
})

it('opens the filtered catalog after sync and preselects the source for new skills', async () => {
  vi.mocked(allSkills).mockResolvedValue([])
  const source = { id: 'source', key: 'tools', label: 'Tools', version: 3, git_url: 'https://example.com/skills.git' }
  vi.mocked(skills.sources).mockResolvedValue([source] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  vi.mocked(skills.sourceSync).mockResolvedValue({ created: 2, updated: 0, unchanged: 1 })
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as { syncSource: (row: unknown) => Promise<void>; edit: (row: null) => void; activeTab: string; sourceFilter: string; form: SkillInput; syncResult: string }
  await vm.syncSource(source)
  expect(skills.sourceSync).toHaveBeenCalledWith(source)
  expect(vm.activeTab).toBe('catalog')
  expect(vm.sourceFilter).toBe('source')
  expect(vm.syncResult).toContain('2')
  vm.edit(null)
  expect(vm.form.source_id).toBe('source')
  wrapper.unmount()
})

it('saves raw environment input directly and retains masked values', async () => {
  vi.mocked(allSkills).mockResolvedValue([])
  vi.mocked(skills.sources).mockResolvedValue([])
  vi.mocked(skills.presets).mockResolvedValue([])
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
  await flushPromises()
  const vm = wrapper.vm as unknown as { editPreset: (row: unknown) => void; savePreset: () => Promise<void> }
  vm.editPreset({ group_key: 'db_erp', label: 'ERP', vars: { DB_PASSWORD: '******mask' }, tags: [], version: 2 })
  await flushPromises()
  const editor = wrapper.findComponent(SkillEnvEditor)
  await editor.get('input[value=raw]').setValue(true)
  await editor.get('textarea').setValue('DB_PASSWORD="******mask"\nAPI_KEY="value#with=equals"')
  await vm.savePreset()
  expect(skills.presetSave).toHaveBeenCalledWith(expect.objectContaining({ group_key: 'db_erp', version: 2, vars: { DB_PASSWORD: '******mask', API_KEY: 'value#with=equals' } }))
  wrapper.unmount()
})
