import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  teams: {
    list: vi.fn().mockResolvedValue([{ id: 't1', slug: 'a', name_zh: '甲', name_ja: null, name_en: null, sort_order: 0, enabled: true, member_count: 2, rules: [{ id: 'r1', platform: null, dept_path_contains: '技术', sort_order: 0 }] }]),
    create: vi.fn(), update: vi.fn(), remove: vi.fn(), replaceRules: vi.fn().mockResolvedValue({}),
  },
}))

import { teams } from '@/api/admin'
// @/api/client is NOT mocked above, so this pulls in the real ApiError class.
import { ApiError } from '@/api/client'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import TeamsPanel from '@/views/TeamsPanel.vue'

describe('TeamsPanel', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('keeps rows in sync when the teams prop arrives after mount', async () => {
    useAuthStore().user = { id: 'me', login_name: 'c', display_name: 'C', role: 'ai_committee', locale: 'zh' } as never
    // Mirrors UsersView: it mounts TeamsPanel with `:teams="teamList"` where teamList
    // starts as [] and is only filled later in onMounted — the panel must react to that,
    // not just snapshot props.teams once at setup.
    const wrapper = mount(TeamsPanel, { props: { teams: [] }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('甲')
    expect(teams.list).not.toHaveBeenCalled()

    await wrapper.setProps({
      teams: [{ id: 't1', slug: 'a', name_zh: '甲', name_ja: null, name_en: null, sort_order: 0, enabled: true, member_count: 2, rules: [] }],
    })
    await flushPromises()
    expect(wrapper.text()).toContain('甲')
  })

  it('lists teams and saves rules', async () => {
    useAuthStore().user = { id: 'me', login_name: 'c', display_name: 'C', role: 'ai_committee', locale: 'zh' } as never
    const wrapper = mount(TeamsPanel, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    expect(wrapper.text()).toContain('甲')
    await wrapper.get('[data-test="rules-t1"]').trigger('click')
    await flushPromises()
    await document.querySelector<HTMLButtonElement>('[data-test="save-rules"]')!.click()
    await flushPromises()
    expect(teams.replaceRules).toHaveBeenCalledWith('t1', [{ platform: null, dept_path_contains: '技术', sort_order: 0 }])
    wrapper.unmount()
  })

  it('hides write buttons for member', async () => {
    useAuthStore().user = { id: 'me', login_name: 'm', display_name: 'M', role: 'member', locale: 'zh' } as never
    const wrapper = mount(TeamsPanel, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="create-team"]').exists()).toBe(false)
  })

  it('always confirms then calls remove, surfacing the backend 409 message even when the team has members', async () => {
    useAuthStore().user = { id: 'me', login_name: 'c', display_name: 'C', role: 'ai_committee', locale: 'zh' } as never
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const errorSpy = vi.spyOn(ElMessage, 'error')
    vi.mocked(teams.remove).mockRejectedValueOnce(new ApiError(409, 409, '团队下仍有用户'))

    const wrapper = mount(TeamsPanel, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    // Fixture team t1 has member_count: 2 — the client must no longer skip the
    // confirm+API call for teams that still have members.
    await wrapper.get('[data-test="delete-team-t1"]').trigger('click')
    await flushPromises()

    expect(confirmSpy).toHaveBeenCalled()
    expect(teams.remove).toHaveBeenCalledWith('t1')
    expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining('团队下仍有用户'))

    confirmSpy.mockRestore()
    errorSpy.mockRestore()
  })
})
