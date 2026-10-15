import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, expect, it, vi } from 'vitest'
const user = vi.hoisted(() => ({ role: 'member' }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user }) }))
vi.mock('@/api/skills', () => ({ allSkills: vi.fn(), skills: { sources: vi.fn(), presets: vi.fn(), localInstall: vi.fn(), envExports: vi.fn() } }))
import { allSkills, skills, type Skill } from '@/api/skills'
import SkillsView from '@/views/SkillsView.vue'
import { i18n } from '@/i18n'

const base = { revision: 1, source_id: 'source', description: '', category: null, security_level: 'public', version: '1.0', env_groups: ['oss'], selectable_env_groups: { db_a: 'A', db_b: 'B' }, data_sources: null, default_data_source: null, doris_enabled_groups: [], user_env_vars: {}, external_repo_url: null, security_prompt_template: null, has_mcp_config: false, mcp_oauth: false }
const git = { ...base, id: 's1', name: 'report', install_type: 'git', enabled: true } as Skill
const mcp = { ...base, id: 's2', name: 'figma', install_type: 'mcp', enabled: true } as Skill

async function view() {
  vi.mocked(allSkills).mockResolvedValue([git, mcp])
  vi.mocked(skills.sources).mockResolvedValue([{ id: 'source', key: 'tools', label: 'Tools' }] as never)
  vi.mocked(skills.presets).mockResolvedValue([])
  vi.mocked(skills.localInstall).mockResolvedValue({ command: "curl -fsSL 'https://coreman.example.com/api/skill-install/abc' | sh", expires_at: '2026-10-15T10:30:00Z' })
  const wrapper = mount(SkillsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
  await flushPromises()
  return wrapper
}
afterEach(() => { document.body.innerHTML = ''; vi.clearAllMocks() })

it('shows members a one-click command for Git skills and hides the environment export', async () => {
  user.role = 'member'
  const wrapper = await view()
  expect(wrapper.find('[data-test="local-install-figma"]').exists()).toBe(false)
  expect(wrapper.find('[data-test="delete-report"]').exists()).toBe(false)
  await wrapper.get('[data-test="local-install-report"]').trigger('click')
  await flushPromises()
  expect(skills.localInstall).toHaveBeenCalledWith(expect.objectContaining({ id: 's1' }))
  const command = document.querySelector<HTMLTextAreaElement>('textarea[data-test="local-install-command"]')
  expect(command?.value).toContain('/api/skill-install/abc')
  expect(document.querySelector('[data-test="env-export"]')).toBeNull()
  wrapper.unmount()
})

it('lets platform administrators reveal plain-text exports for the selected databases', async () => {
  user.role = 'platform_admin'
  vi.mocked(skills.envExports).mockResolvedValue({ text: "export DB_PASSWORD='secret'\n" })
  const wrapper = await view()
  await wrapper.get('[data-test="local-install-figma"]').trigger('click')
  await flushPromises()
  // MCP 技能没有安装命令，但管理员仍能导出它的环境变量。
  expect(skills.localInstall).not.toHaveBeenCalled()
  document.querySelector<HTMLInputElement>('[data-test="env-export"] input[type="checkbox"][value="db_b"]')!.click()
  await flushPromises()
  document.querySelector<HTMLButtonElement>('[data-test="env-export-reveal"]')!.click()
  await flushPromises()
  expect(skills.envExports).toHaveBeenCalledWith(expect.objectContaining({ id: 's2' }), { selected_env_groups: ['db_b'], data_source: null })
  const text = document.querySelector<HTMLTextAreaElement>('textarea[data-test="env-export-text"]')
  expect(text?.value).toContain("export DB_PASSWORD='secret'")
  wrapper.unmount()
})
