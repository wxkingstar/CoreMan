import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

// vi.mock() 工厂会被提升到模块顶部，直接引用模块级 const 会 ReferenceError；
// vi.hoisted() 先于它执行，返回值可以安全地在工厂里用。
const { bot } = vi.hoisted(() => {
  const perms = { role: 'creator', can_view_sensitive: true, can_view_env_full: false, can_edit: true, can_switch_relay: true, can_toggle: true, can_delete: true, can_manage_members: true, can_reassign_team: false }
  return {
    bot: { id: 'b1', bot_key: 'sales_bot', platform: 'wecom', name: '销售助手', description: '', avatar_url: null, enabled: true, team_id: 't1', team_name: '销售', created_by: 'me', created_by_name: 'U', relay_server_id: 'r1', relay_name: 'claude01', relay_url: 'http://h:1', model: 'vllm/claude-sonnet-4-6', backend: 'claude', working_dir: '/data/skills/sales_bot', verbosity_level: 1, effort_level: null, sse_timeout_seconds: 3600, welcome_message: null, rich_cards: true, dev_mode: false, notify_webhook_url: null, member_count: 0, allowed_user_count: 0, permissions: perms, version: 1, created_at: '', updated_at: '' },
  }
})

vi.mock('@/api/admin', () => ({
  bots: { list: vi.fn().mockResolvedValue({ items: [bot], total: 1, page: 1, per_page: 50 }), toggle: vi.fn().mockResolvedValue({ ...bot, enabled: false }), remove: vi.fn() },
  relays: { list: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 200 }) },
  catalog: { list: vi.fn().mockResolvedValue([]) },
}))

import { bots, catalog } from '@/api/admin'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import BotsView from '@/views/BotsView.vue'

describe('BotsView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('lists bots with scope=mine and toggles', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: BotsView }, { path: '/bots/:id', name: 'bot-detail', component: { template: '<div />' } }] })
    const wrapper = mount(BotsView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(bots.list).toHaveBeenCalledWith(expect.objectContaining({ scope: 'mine', page: 1, per_page: 50 }))
    expect(wrapper.text()).toContain('销售助手')
    expect(wrapper.text()).toContain('claude01')
    await wrapper.get('[data-test="toggle-b1"]').trigger('click')
    await flushPromises()
    expect(bots.toggle).toHaveBeenCalledWith('b1')
  })

  it('opens the switch dialog in place, stays on the list and reloads after switching', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: BotsView }, { path: '/bots/:id', name: 'bot-detail', component: { template: '<div />' } }] })
    await router.push('/')
    const wrapper = mount(BotsView, { global: { plugins: [ElementPlus, i18n, router], stubs: { SwitchRelayDialog: { name: 'SwitchRelayDialog', props: ['bot', 'visible'], template: '<div data-test="switch-dialog" />' } } } })
    await flushPromises()
    vi.mocked(bots.list).mockClear()
    await wrapper.get('[data-test="switch-b1"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    const dialog = wrapper.findComponent({ name: 'SwitchRelayDialog' })
    expect(dialog.props('bot')).toMatchObject({ id: 'b1' })
    dialog.vm.$emit('switched', { old_relay_id: 'r1', new_relay_id: 'r2', old_model: bot.model, new_model: bot.model, bot })
    dialog.vm.$emit('update:visible', false)
    await flushPromises()
    expect(bots.list).toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/')
    expect(wrapper.find('[data-test="switch-dialog"]').exists()).toBe(false)
  })

  it('model filter lists only models that are not retired', async () => {
    vi.mocked(catalog.list).mockResolvedValueOnce([
      { provider: 'codex', model: 'codex/gpt-6-astra', display_name: null, is_default: false, retired: false, supports_xhigh: false, supports_max: false, sort_order: 1, backend: 'codex' },
      { provider: 'codex', model: 'codex/gpt-5-old', display_name: null, is_default: false, retired: true, supports_xhigh: false, supports_max: false, sort_order: 2, backend: 'codex' },
    ])
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: BotsView }, { path: '/bots/:id', name: 'bot-detail', component: { template: '<div />' } }] })
    const wrapper = mount(BotsView, { attachTo: document.body, global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    const values = wrapper.findAllComponents({ name: 'ElOption' }).map((o) => o.props('value'))
    expect(values).toContain('codex/gpt-6-astra')
    expect(values).not.toContain('codex/gpt-5-old')
    wrapper.unmount()
  })
})
