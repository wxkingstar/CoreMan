import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

const perms = (over: Record<string, unknown> = {}) => ({
  role: 'creator', can_view_sensitive: true, can_view_env_full: false, can_edit: true,
  can_switch_relay: true, can_toggle: true, can_delete: true, can_manage_members: true,
  can_reassign_team: false, ...over,
})
const bot = (over: Record<string, unknown> = {}) => ({
  id: 'b1', bot_key: 'sales_bot', platform: 'wecom', name: '销售助手', description: '', avatar_url: null,
  enabled: true, team_id: 't1', team_name: '销售', created_by: 'me', created_by_name: 'U',
  relay_server_id: 'r1', relay_name: 'claude01', relay_url: 'http://h:1', model: 'vllm/claude-sonnet-4-6',
  backend: 'claude', working_dir: '/data/skills/sales_bot', verbosity_level: 1, effort_level: null,
  sse_timeout_seconds: 3600, welcome_message: null, notify_webhook_url: 'ht••••-1',
  member_count: 1, allowed_user_count: 0, permissions: perms(), version: 1,
  created_at: '2026-09-10T00:00:00Z', updated_at: '2026-09-10T00:00:00Z', system_prompt: '你是销售',
  credentials: { bot_id: 'we••••-1', secret: 'we••••ue' }, env_vars: { DB_PASSWORD: 'pw••••56' }, ...over,
})

vi.mock('@/api/admin', () => ({
  bots: {
    get: vi.fn(),
    members: vi.fn().mockResolvedValue([{ user_id: 'u1', login_name: 'lisi', display_name: '李四', added_at: '', added_by: 'me' }]),
    allowedUsers: vi.fn().mockResolvedValue([]),
    addMember: vi.fn(),
    removeMember: vi.fn().mockResolvedValue(null),
    toggle: vi.fn(),
    remove: vi.fn(),
    setAllowedUsers: vi.fn(),
  },
  relays: { list: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 200 }) },
  users: { list: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 }) },
  catalog: { list: vi.fn().mockResolvedValue([]) },
  settings: { defaults: vi.fn().mockResolvedValue({}) },
  teams: { list: vi.fn().mockResolvedValue([]) },
}))

import { bots } from '@/api/admin'
import UserPicker from '@/components/UserPicker.vue'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import BotDetailView from '@/views/BotDetailView.vue'
import SwitchRelayDialog from '@/views/SwitchRelayDialog.vue'

let router: ReturnType<typeof createRouter>

async function mountDetail(data: ReturnType<typeof bot>, path = '/bots/b1') {
  vi.mocked(bots.get).mockResolvedValue(data as never)
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/bots', name: 'bots', component: { template: '<div />' } },
      { path: '/bots/:id', name: 'bot-detail', component: BotDetailView },
    ],
  })
  await router.push(path)
  await router.isReady()
  const wrapper = mount(BotDetailView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
  await flushPromises()
  return wrapper
}

describe('BotDetailView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('shows sensitive fields for creator and manages members', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    const wrapper = await mountDetail(bot())
    expect(wrapper.text()).toContain('你是销售')
    expect(wrapper.text()).toContain('we••••ue')
    // 通知目的地已移到定时任务，员工详情不再展示。
    expect(wrapper.text()).not.toContain('ht••••-1')
    await wrapper.get('[data-test="open-members"]').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('李四')
    document.querySelector<HTMLButtonElement>('[data-test="remove-member-u1"]')!.click()
    await flushPromises()
    expect(bots.removeMember).toHaveBeenCalledWith('b1', 'u1')
    wrapper.unmount()
  })

  it('hides sensitive section and write buttons for outsiders', async () => {
    useAuthStore().user = { id: 'x', login_name: 'x', display_name: 'X', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    const data = bot({ permissions: perms({ role: null, can_view_sensitive: false, can_edit: false, can_switch_relay: false, can_toggle: false, can_delete: false, can_manage_members: false }) })
    delete (data as Record<string, unknown>).system_prompt
    delete (data as Record<string, unknown>).credentials
    delete (data as Record<string, unknown>).env_vars
    delete (data as Record<string, unknown>).notify_webhook_url
    const wrapper = await mountDetail(data)
    expect(wrapper.text()).not.toContain(i18n.global.t('bots.detail.sensitive'))
    expect(wrapper.text()).not.toContain('ht••••-1')
    expect(wrapper.find('[data-test="edit-bot"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="delete-bot"]').exists()).toBe(false)
    wrapper.unmount()
  })

  // 后端 build_out 里 env_vars_full 只看 can_view_env_full，与 can_view_sensitive 无关：
  // AI 委员会成员既不是创建者也不是协作者时，只会拿到明文这一块，页面不能把它一起藏掉。
  it('shows the plaintext env table when only can_view_env_full is granted', async () => {
    useAuthStore().user = { id: 'c', login_name: 'c', display_name: 'C', role: 'ai_committee', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    const data = bot({
      permissions: perms({
        role: null, can_view_sensitive: false, can_view_env_full: true, can_edit: false,
        can_delete: false, can_manage_members: false, can_reassign_team: true,
      }),
      env_vars_full: { DB_PASSWORD: 'pw-123456' },
    })
    delete (data as Record<string, unknown>).system_prompt
    delete (data as Record<string, unknown>).credentials
    delete (data as Record<string, unknown>).env_vars
    delete (data as Record<string, unknown>).notify_webhook_url
    const wrapper = await mountDetail(data)
    expect(wrapper.text()).toContain('pw-123456')
    expect(wrapper.text()).toContain(i18n.global.t('bots.detail.envVarsFull'))
    // 脱敏那三块后端没下发，不能凭空渲染出空标题。
    expect(wrapper.text()).not.toContain(i18n.global.t('bots.detail.systemPrompt'))
    wrapper.unmount()
  })

  it('auto-opens the switch dialog from ?action=switch, drops the query and applies the result', async () => {
    const wrapper = await mountDetail(bot(), '/bots/b1?action=switch')
    const dialog = wrapper.findComponent(SwitchRelayDialog)
    expect(dialog.exists()).toBe(true)
    // query 留着的话，刷新页面会再弹一次。
    expect(router.currentRoute.value.fullPath).toBe('/bots/b1')
    dialog.vm.$emit('switched', {
      old_relay_id: 'r1', new_relay_id: 'r2',
      old_model: 'vllm/claude-sonnet-4-6', new_model: 'codex/gpt-5.5',
      bot: bot({ relay_server_id: 'r2', relay_name: 'codex01', model: 'codex/gpt-5.5', backend: 'codex', version: 2 }),
    })
    await flushPromises()
    expect(wrapper.text()).toContain('codex01')
    expect(wrapper.text()).toContain('codex/gpt-5.5')
    wrapper.unmount()
  })

  it('replaces the allowed-user set from the picker', async () => {
    vi.mocked(bots.setAllowedUsers).mockResolvedValue([{ user_id: 'u9', login_name: 'wang', display_name: '王九' }])
    const wrapper = await mountDetail(bot())
    await wrapper.get('[data-test="open-allowed"]').trigger('click')
    await flushPromises()
    wrapper.findComponent(UserPicker).vm.$emit('update:modelValue', ['u9'])
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-test="save-allowed"]')!.click()
    await flushPromises()
    expect(bots.setAllowedUsers).toHaveBeenCalledWith('b1', ['u9'])
    wrapper.unmount()
  })
})
