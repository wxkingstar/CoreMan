import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// vi.mock() 工厂会被提升到模块顶部，直接引用模块级 const 会 ReferenceError；
// vi.hoisted() 先于它执行，返回值可以安全地在工厂里用（同 PlatformAppsView.spec）。
const { relay } = vi.hoisted(() => ({
  relay: { id: 'r1', name: 'claude01', host: '10.0.0.1', clawrelay_port: 50009, agent_port: 52123, relay_url: 'http://10.0.0.1:50009', ssh_user: 'claude01', runtime_env: 'chroot', chroot_path: null, runtime_user: null, model_provider: 'claude', supported_models_mode: 'inherit', supported_models: null, effective_models: ['vllm/claude-sonnet-4-6'], default_model: 'vllm/claude-sonnet-4-6', team_id: null, team_name: null, visibility: 'all', description: null, is_active: true, has_agent_token: false, rate_limit_5h_used_pct: 42.5, rate_limit_5h_resets_at: null, rate_limit_7d_used_pct: null, rate_limit_7d_resets_at: null, rate_limit_probed_at: null, health_status: 'healthy', health_checked_at: '2026-09-10T00:00:00Z', health_detail: null, health_latency_ms: 12, health_fail_count: 0, relay_version: '2.2.0', relay_mode: 'v1', bot_count: 3, version: 1, created_at: '', updated_at: '' },
}))

vi.mock('@/api/admin', () => ({
  relays: { list: vi.fn().mockResolvedValue({ items: [relay], total: 1, page: 1, per_page: 50 }), probe: vi.fn().mockResolvedValue({ health: { status: 'down', detail: 'HTTP 503', latency_ms: 5 }, added_models: [], relay: { ...relay, health_status: 'down' } }), teamLoad: vi.fn().mockResolvedValue({ teams: [], unassigned_user_count: 0, unassigned_bot_count: 0 }), update: vi.fn(), remove: vi.fn(), create: vi.fn(), get: vi.fn(), models: vi.fn(), agentToken: vi.fn().mockResolvedValue({ token: 'tok-1234567890' }) },
  catalog: { list: vi.fn().mockResolvedValue([]) },
  teams: { list: vi.fn().mockResolvedValue([]) },
}))

import { relays } from '@/api/admin'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import RelaysView from '@/views/RelaysView.vue'

describe('RelaysView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('renders relay rows and probes on click (managers only see write buttons)', async () => {
    useAuthStore().user = { id: 'me', login_name: 'c', display_name: 'C', role: 'ai_committee', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    const wrapper = mount(RelaysView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).toContain('claude01')
    expect(wrapper.text()).toContain('CL:50009')
    expect(wrapper.find('[data-test="create-relay"]').exists()).toBe(true)
    await wrapper.get('[data-test="probe-r1"]').trigger('click')
    await flushPromises()
    expect(relays.probe).toHaveBeenCalledWith('r1')
    expect(wrapper.text()).toContain(i18n.global.t('relays.health.down'))
  })

  it('hides write buttons for member', async () => {
    useAuthStore().user = { id: 'me', login_name: 'm', display_name: 'M', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    const wrapper = mount(RelaysView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="create-relay"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="probe-r1"]').exists()).toBe(false)
  })

  // jsdom（以及 http 这种非安全上下文）里没有 navigator.clipboard，复制必须报失败而不是假成功。
  it('warns instead of claiming success when the clipboard API is missing', async () => {
    useAuthStore().user = { id: 'me', login_name: 'c', display_name: 'C', role: 'ai_committee', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const success = vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
    const wrapper = mount(RelaysView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    await wrapper.get('[data-test="token-r1"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="copy-token"]').trigger('click')
    await flushPromises()
    expect(warning).toHaveBeenCalledWith(i18n.global.t('relays.copyFailed'))
    expect(success).not.toHaveBeenCalled()
    // 不能用 vi.restoreAllMocks()：那会把 vi.mock 工厂里的 vi.fn() 实现一并清掉。
    warning.mockRestore()
    success.mockRestore()
  })

  // 签发令牌会 bump version：不把整行刷新掉，下一次编辑/停用带的还是旧 If-Match（409）。
  it('refreshes the row after issuing a token so the next write carries the new version', async () => {
    useAuthStore().user = { id: 'me', login_name: 'c', display_name: 'C', role: 'ai_committee', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    vi.mocked(relays.get).mockResolvedValue({ ...relay, version: 2, has_agent_token: true } as never)
    vi.mocked(relays.update).mockResolvedValue({ ...relay, version: 3 } as never)
    const wrapper = mount(RelaysView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    await wrapper.get('[data-test="token-r1"]').trigger('click')
    await flushPromises()
    expect(relays.get).toHaveBeenCalledWith('r1')
    await wrapper.get('[data-test="active-r1"]').trigger('click')
    await flushPromises()
    expect(vi.mocked(relays.update).mock.calls[0][2]).toBe(2)
  })
})
