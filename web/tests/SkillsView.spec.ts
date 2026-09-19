import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { expect, it, vi } from 'vitest'
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user: { role: 'ai_committee' } }) }))
vi.mock('@/api/skills', () => ({ allSkills: vi.fn(), skills: { sources: vi.fn(), presets: vi.fn(), save: vi.fn(), setEnabled: vi.fn(), remove: vi.fn(), presetSave: vi.fn(), sourceSync: vi.fn(), sourceSave: vi.fn() } }))
import { allSkills, skills, type Skill, type SkillInput } from '@/api/skills'
import SkillsView from '@/views/SkillsView.vue'
import SkillEnvEditor from '@/components/SkillEnvEditor.vue'
import SkillEditorDialog from '@/components/skills/SkillEditorDialog.vue'
import SkillPresetDialog from '@/components/skills/SkillPresetDialog.vue'
import SkillSourceDialog from '@/components/skills/SkillSourceDialog.vue'
import { i18n } from '@/i18n'

it('preserves encrypted MCP configuration and security fields when editing catalog metadata', async () => {
  const row = { id: 's1', revision: 4, name: 'query', source_id: 'source', description: 'old', category: null, security_level: 'internal', version: '1.0', env_groups: [], selectable_env_groups: {}, data_sources: null, default_data_source: null, doris_enabled_groups: [], user_env_vars: {}, install_type: 'mcp', external_repo_url: null, security_prompt_template: 'Read only', enabled: true, has_mcp_config: true } as Skill
  vi.mocked(allSkills).mockResolvedValue([row])
  vi.mocked(skills.sources).mockResolvedValue([{ id: 'source', key: 'tools', label: 'Tools' }] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  vi.mocked(skills.save).mockResolvedValue(row)
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
  await flushPromises()
  const vm = wrapper.vm as unknown as { edit: (row: Skill) => void; rows: Skill[] }
  vm.edit(vm.rows[0]!); await flushPromises()
  const dialog = wrapper.findComponent(SkillEditorDialog).vm as unknown as { form: SkillInput; save: () => Promise<void> }
  dialog.form.description = 'new'
  await dialog.save(); await flushPromises()
  expect(skills.save).toHaveBeenCalledWith(expect.objectContaining({ revision: 4 }), expect.objectContaining({ description: 'new', mcp_config: null, security_prompt_template: 'Read only', security_level: 'internal' }))
  wrapper.unmount()
})

it('toggles skill status inline and reloads the catalog when the revision is stale', async () => {
  const row = { id: 's1', revision: 2, name: 'sample-query', source_id: 'source', description: '', category: null, security_level: 'public', version: '1.0', env_groups: ['shared_tools'], selectable_env_groups: {}, data_sources: null, default_data_source: null, doris_enabled_groups: [], user_env_vars: {}, install_type: 'git', external_repo_url: null, security_prompt_template: null, enabled: false, has_mcp_config: false } as Skill
  vi.mocked(allSkills).mockResolvedValue([{ ...row }])
  vi.mocked(skills.sources).mockResolvedValue([{ id: 'source', key: 'tools', label: 'Tools' }] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  // 行对象会被原地更新，按调用时刻记下修订号。
  const sent: [string, number, boolean][] = []
  vi.mocked(skills.setEnabled).mockImplementation(async (target, enabled) => {
    sent.push([target.id, target.revision, enabled])
    if (sent.length > 1) throw new Error('stale')
    return { ...target, enabled, revision: target.revision + 1 }
  })
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  await wrapper.get('[data-test="enabled-sample-query"]').trigger('click')
  await flushPromises()
  const vm = wrapper.vm as unknown as { rows: Skill[] }
  expect(vm.rows[0]).toMatchObject({ enabled: true, revision: 3 })
  vi.mocked(allSkills).mockClear()
  await wrapper.get('[data-test="enabled-sample-query"]').trigger('click')
  await flushPromises()
  expect(sent).toEqual([['s1', 2, true], ['s1', 3, false]])
  expect(allSkills).toHaveBeenCalledTimes(1)
  wrapper.unmount()
})

it('deletes a skill only after confirmation and reloads the catalog either way', async () => {
  const row = { id: 's1', revision: 5, name: 'unwanted', source_id: 'source', description: '', category: null, security_level: 'public', version: '1.0', env_groups: [], selectable_env_groups: {}, data_sources: null, default_data_source: null, doris_enabled_groups: [], user_env_vars: {}, install_type: 'git', external_repo_url: null, security_prompt_template: null, enabled: true, has_mcp_config: false } as Skill
  vi.mocked(allSkills).mockResolvedValue([row])
  vi.mocked(skills.sources).mockResolvedValue([{ id: 'source', key: 'tools', label: 'Tools' }] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  vi.mocked(skills.remove).mockResolvedValue(undefined as never)
  const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  await wrapper.get('[data-test="delete-unwanted"]').trigger('click')
  await flushPromises()
  expect(confirm.mock.calls[0]![0]).toContain('unwanted')
  expect(skills.remove).not.toHaveBeenCalled()
  confirm.mockResolvedValue('confirm' as never)
  vi.mocked(allSkills).mockClear().mockResolvedValue([])
  await wrapper.get('[data-test="delete-unwanted"]').trigger('click')
  await flushPromises()
  expect(skills.remove).toHaveBeenCalledWith(expect.objectContaining({ id: 's1', revision: 5 }))
  expect(allSkills).toHaveBeenCalledTimes(1)
  expect(wrapper.find('[data-test="delete-unwanted"]').exists()).toBe(false)
  confirm.mockRestore()
  wrapper.unmount()
})

it('opens the filtered catalog after sync and preselects the source for new skills', async () => {
  vi.mocked(allSkills).mockResolvedValue([])
  const source = { id: 'source', key: 'tools', label: 'Tools', version: 3, git_url: 'https://example.com/skills.git' }
  vi.mocked(skills.sources).mockResolvedValue([source] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  vi.mocked(skills.sourceSync).mockResolvedValue({ created: 2, updated: 0, unchanged: 1, skipped: 0 })
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as { syncSource: (row: unknown) => Promise<void>; edit: (row: null) => void; activeTab: string; sourceFilter: string; syncResult: string }
  await vm.syncSource(source)
  expect(skills.sourceSync).toHaveBeenCalledWith(source)
  expect(vm.activeTab).toBe('catalog')
  expect(vm.sourceFilter).toBe('source')
  expect(vm.syncResult).toContain('2')
  expect(vm.syncResult).not.toContain(i18n.global.t('skillEditor.syncSkipped', { skipped: 0 }))
  vi.mocked(skills.sourceSync).mockResolvedValue({ created: 0, updated: 0, unchanged: 1, skipped: 2 })
  await vm.syncSource(source)
  expect(vm.syncResult).toContain(i18n.global.t('skillEditor.syncSkipped', { skipped: 2 }))
  vm.edit(null)
  expect((wrapper.findComponent(SkillEditorDialog).vm as unknown as { form: SkillInput }).form.source_id).toBe('source')
  wrapper.unmount()
})

it('saves raw environment input directly and retains masked values', async () => {
  vi.mocked(allSkills).mockResolvedValue([])
  vi.mocked(skills.sources).mockResolvedValue([])
  vi.mocked(skills.presets).mockResolvedValue([])
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
  await flushPromises()
  const vm = wrapper.vm as unknown as { editPreset: (row: unknown) => void }
  vm.editPreset({ group_key: 'db_erp', label: 'ERP', vars: { DB_PASSWORD: '******mask' }, tags: [], version: 2 })
  await flushPromises()
  const editor = wrapper.findComponent(SkillEnvEditor)
  await editor.get('input[value=raw]').setValue(true)
  await editor.get('textarea').setValue('DB_PASSWORD="******mask"\nAPI_KEY="value#with=equals"')
  await (wrapper.findComponent(SkillPresetDialog).vm as unknown as { savePreset: () => Promise<void> }).savePreset()
  expect(skills.presetSave).toHaveBeenCalledWith(expect.objectContaining({ group_key: 'db_erp', version: 2, vars: { DB_PASSWORD: '******mask', API_KEY: 'value#with=equals' } }))
  wrapper.unmount()
})


it('keeps stored project tokens out of the form and clears newly entered tokens after save', async () => {
  vi.mocked(allSkills).mockResolvedValue([])
  const source = { id: 'source', key: 'tools', label: 'Tools', version: 3, git_url: 'https://git.example.com/tools.git', has_access_token: true }
  vi.mocked(skills.sources).mockResolvedValue([source] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  vi.mocked(skills.sourceSave).mockResolvedValue(source as never)
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as { editSource: (row: unknown) => void }
  const dialog = wrapper.findComponent(SkillSourceDialog).vm as unknown as { saveSource: () => Promise<void>; sourceForm: { access_token: string; remove_access_token: boolean } }
  vm.editSource(source)
  expect(dialog.sourceForm.access_token).toBe('')
  expect(dialog.sourceForm.remove_access_token).toBe(false)
  await dialog.saveSource()
  expect(skills.sourceSave).toHaveBeenCalledWith(source, expect.objectContaining({ access_token: '', remove_access_token: false }))
  vm.editSource(source)
  dialog.sourceForm.access_token = 'test-token'
  await dialog.saveSource()
  expect(skills.sourceSave).toHaveBeenLastCalledWith(source, expect.objectContaining({ access_token: 'test-token' }))
  expect(dialog.sourceForm.access_token).toBe('')
  wrapper.unmount()
})
