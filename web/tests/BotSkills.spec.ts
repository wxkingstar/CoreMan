import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, expect, it, vi } from 'vitest'
vi.mock('@/api/skills', () => ({ allSkills: vi.fn(), skills: { installed: vi.fn(), install: vi.fn(), uninstall: vi.fn() } }))
import { allSkills, skills } from '@/api/skills'
import BotSkills from '@/components/BotSkills.vue'
import { i18n } from '@/i18n'

beforeEach(() => vi.clearAllMocks())
it('loads only on open and submits a scope without marking the skill installed', async () => {
  const item = { id: 's1', name: 'query', description: 'Query', enabled: true, security_level: 'internal', selectable_env_groups: { erp: 'ERP' }, user_env_vars: {}, security_prompt_template: 'Read only', default_data_source: null }
  vi.mocked(allSkills).mockResolvedValue([item] as never)
  vi.mocked(skills.installed).mockResolvedValue({ items: [], pending_approvals: [] })
  vi.mocked(skills.install).mockResolvedValue({ status: 'pending_approval' })
  const wrapper = mount(BotSkills, { props: { botId: 'b1' }, global: { plugins: [ElementPlus, i18n], stubs: { teleport: true } } })
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
