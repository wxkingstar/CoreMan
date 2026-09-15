import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// vi.mock() 的工厂会被提升到模块顶层 const 之上，用 vi.hoisted 才能在工厂里安全引用。
const { mockUser } = vi.hoisted(() => ({
  mockUser: { id: 'u1', login_name: 'zhangsan', display_name: '张三', email: 'zs@example.com', mobile: null, avatar_url: null,
    status: 'active', locale: 'zh', role: 'member', source: 'sync', team_id: null, team_name: null, position: null,
    skills: null, bot_accessible: true, manual_fields: [], last_login_at: null, identities: [], departments: ['公司/技术'] },
}))

vi.mock('@/api/admin', () => ({
  users: {
    list: vi.fn().mockResolvedValue({ items: [mockUser], total: 1, page: 1, per_page: 50 }),
    patch: vi.fn().mockImplementation(async (_id: string, body: Record<string, unknown>) => ({ ...body })),
  },
  teams: { list: vi.fn().mockResolvedValue([{ id: 't1', slug: 'a', name_zh: '甲', name_ja: null, name_en: null, sort_order: 0, enabled: true, member_count: 0, rules: [] }]) },
  departments: { tree: vi.fn().mockResolvedValue([]) },
}))

import { users } from '@/api/admin'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import UsersView from '@/views/UsersView.vue'

describe('UsersView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('renders users and lets platform_admin change team inline', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'me', login_name: 'admin', display_name: 'A', role: 'platform_admin', locale: 'zh' } as never
    const wrapper = mount(UsersView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).toContain('张三')
    expect(wrapper.text()).toContain('公司/技术')
    expect(wrapper.find('[data-test="team-select-u1"]').exists()).toBe(true)
    expect(users.list).toHaveBeenCalledWith(expect.objectContaining({ page: 1, per_page: 50 }))
  })

  it('only submits the fields the drawer actually changed', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'me', login_name: 'admin', display_name: 'A', role: 'platform_admin', locale: 'zh' } as never
    const wrapper = mount(UsersView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="edit-u1"]').trigger('click')
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('[data-test="edit-position"] input')!
    input.value = '研发'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-test="save-edit"]')!.click()
    await flushPromises()
    // locale/skills/status 没动过就不能出现在 body 里：后端会把它们记进 manual_fields，
    // 之后通讯录同步就永远不再刷新这些字段了。
    const calls = vi.mocked(users.patch).mock.calls
    expect(calls[calls.length - 1]).toEqual(['u1', { position: '研发' }])
    wrapper.unmount()
  })

  it('hides the status control from team_lead', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'me', login_name: 'lead', display_name: 'L', role: 'team_lead', team_id: 't1', locale: 'zh' } as never
    vi.mocked(users.list).mockResolvedValueOnce({ items: [{ ...mockUser, team_id: 't1', team_name: '甲' }], total: 1, page: 1, per_page: 50 } as never)
    const wrapper = mount(UsersView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="edit-u1"]').trigger('click')
    await flushPromises()
    expect(document.querySelector('[data-test="edit-position"]')).not.toBeNull()
    expect(document.querySelector('[data-test="edit-status"]')).toBeNull()
    wrapper.unmount()
  })

  it('still loads the user list and reports the error when the team list fails', async () => {
    const { teams } = await import('@/api/admin')
    const { ElMessage } = await import('element-plus')
    const error = vi.spyOn(ElMessage, 'error').mockImplementation(() => ({ close: () => {} }) as never)
    vi.mocked(teams.list).mockRejectedValueOnce(new Error('teams unavailable'))
    vi.mocked(users.list).mockClear()
    const auth = useAuthStore()
    auth.user = { id: 'me', login_name: 'admin', display_name: 'A', role: 'platform_admin', locale: 'zh' } as never
    const wrapper = mount(UsersView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(error).toHaveBeenCalledWith('teams unavailable')
    expect(users.list).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('张三')
    error.mockRestore()
    wrapper.unmount()
  })

  it('member sees read-only rows', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'me', login_name: 'm', display_name: 'M', role: 'member', locale: 'zh' } as never
    const wrapper = mount(UsersView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="team-select-u1"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="edit-u1"]').exists()).toBe(false)
  })

  // 非管理角色拿到的用户只有基础字段（后端按角色裁剪）。
  const restrictedUser = {
    id: 'u1', login_name: 'zhangsan', display_name: '张三', avatar_url: null, status: 'active', role: 'member',
    team_id: 't1', team_name: '甲', source: 'sync', locale: 'zh',
  }

  it('hides detail columns the backend withholds from non-manager roles', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'me', login_name: 'lead', display_name: 'L', role: 'team_lead', team_id: 't1', locale: 'zh' } as never
    vi.mocked(users.list).mockResolvedValueOnce({ items: [restrictedUser], total: 1, page: 1, per_page: 50 } as never)
    const wrapper = mount(UsersView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).toContain('张三')
    const headers = wrapper.findAll('th').map((th) => th.text())
    for (const key of ['users.email', 'users.departments', 'users.botAccessible', 'users.lastLogin']) {
      expect(headers).not.toContain(i18n.global.t(key))
    }
    expect(headers).toContain(i18n.global.t('users.team'))
    wrapper.unmount()
  })

  it('keeps detail columns for managers', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'me', login_name: 'admin', display_name: 'A', role: 'ai_committee', locale: 'zh' } as never
    const wrapper = mount(UsersView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    const headers = wrapper.findAll('th').map((th) => th.text())
    for (const key of ['users.email', 'users.departments', 'users.botAccessible', 'users.lastLogin']) {
      expect(headers).toContain(i18n.global.t(key))
    }
    wrapper.unmount()
  })

  // team_lead 编辑本团队成员时看不到职位原值：输入框留空、没改不提交，改了只提交这一项。
  it('lets team_lead fill a withheld field without clearing untouched ones', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'me', login_name: 'lead', display_name: 'L', role: 'team_lead', team_id: 't1', locale: 'zh' } as never
    vi.mocked(users.list).mockResolvedValueOnce({ items: [{ ...restrictedUser }], total: 1, page: 1, per_page: 50 } as never)
    vi.mocked(users.patch).mockClear()
    const wrapper = mount(UsersView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="edit-u1"]').trigger('click')
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('[data-test="edit-position"] input')!
    expect(input.value).toBe('')
    document.querySelector<HTMLButtonElement>('[data-test="save-edit"]')!.click()
    await flushPromises()
    expect(users.patch).not.toHaveBeenCalled()
    await wrapper.get('[data-test="edit-u1"]').trigger('click')
    await flushPromises()
    input.value = '研发'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-test="save-edit"]')!.click()
    await flushPromises()
    expect(vi.mocked(users.patch).mock.calls).toEqual([['u1', { position: '研发' }]])
    wrapper.unmount()
  })
})
