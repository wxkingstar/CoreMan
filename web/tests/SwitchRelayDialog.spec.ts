import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { describe, expect, it, vi } from 'vitest'

// vi.mock() 的工厂会被提升到模块顶部，直接引用模块级 const 会 ReferenceError；
// vi.hoisted() 先于它执行，返回值可以安全地在工厂里用（同 BotsView.spec）。
const { relayRow } = vi.hoisted(() => ({
  relayRow: (id: string, provider: string, def: string) => ({
    unavailable_reason: null,
    id, name: id, runtime_node_id: 'n1', runtime_name: 'node-1', relay_url: 'http://h:1', model_provider: provider,
    default_model: def, effective_models: [def], team_id: null, team_name: null, is_active: true,
    health_status: 'healthy', health_checked_at: null, health_detail: null,
    rate_limit_5h_used_pct: 10, rate_limit_7d_used_pct: null, version: 1,
  }),
}))

vi.mock('@/api/admin', () => ({
  relays: {
    list: vi.fn().mockResolvedValue({
      items: [relayRow('r1', 'claude', 'vllm/claude-sonnet-4-6'), relayRow('r2', 'codex', 'codex/gpt-5.5')],
      total: 2, page: 1, per_page: 200,
    }),
    models: vi.fn().mockImplementation(async (id: string) => id === 'r2'
      ? { provider: 'codex', mode: 'inherit', models: ['codex/gpt-5.5'], default: 'codex/gpt-5.5' }
      : { provider: 'claude', mode: 'inherit', models: ['vllm/claude-sonnet-4-6'], default: 'vllm/claude-sonnet-4-6' }),
  },
  bots: {
    get: vi.fn().mockResolvedValue({ id: 'b1', version: 7 }),
    switchRelay: vi.fn().mockResolvedValue({
      old_relay_id: 'r1', new_relay_id: 'r2',
      old_model: 'vllm/claude-sonnet-4-6', new_model: 'codex/gpt-5.5',
      bot: { id: 'b1', version: 2 },
    }),
  },
}))

vi.mock('@/api/workspace', () => ({ workspace: { preview: vi.fn().mockResolvedValue({ directory: '/home/ai/project', exists: false, empty: true, owned: false, source_online: true, git_configured: false, memory_snapshot_at: null }), get: vi.fn().mockResolvedValue({ state: 'migrating' }) } }))
import { workspace } from '@/api/workspace'
import { bots, relays } from '@/api/admin'
import { ApiError } from '@/api/client'
import { VERSION_CONFLICT_CODE } from '@/utils/errors'
import { i18n } from '@/i18n'
import SwitchRelayDialog from '@/views/SwitchRelayDialog.vue'

describe('SwitchRelayDialog', () => {
  it('disables a logged-out backend even when it advertises models', async () => {
    vi.mocked(relays.list).mockResolvedValueOnce({ items: [
      relayRow('r1', 'claude', 'vllm/claude-sonnet-4-6'),
      { ...relayRow('r2', 'codex', 'codex/gpt-5.5'), unavailable_reason: 'login_required' },
    ] } as never)
    vi.mocked(bots.switchRelay).mockClear()
    const wrapper = mount(SwitchRelayDialog, { props: { bot: { id: 'b1', version: 1, relay_server_id: 'r1', model: 'vllm/claude-sonnet-4-6' } as never, visible: true }, global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    expect(document.querySelector('[data-test="pick-r2"] input')!.hasAttribute('disabled')).toBe(true)
    expect(document.body.textContent).toContain('未登录')
    await (wrapper.vm as unknown as { pick: (id: string) => Promise<void> }).pick('r2')
    await (wrapper.vm as unknown as { confirm: () => Promise<void> }).confirm()
    expect(bots.switchRelay).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  // 挂载时会预选当前 relay（「同一台只换模型」的入口），此时直接确认就是空操作：
  // 后端照样写一条 diff 为空的 bot.switch_relay 审计，所以前端要拦住。
  it('refuses to submit while neither relay nor model changed', async () => {
    vi.mocked(bots.switchRelay).mockClear()
    const bot0 = { id: 'b1', version: 1, relay_server_id: 'r1', model: 'vllm/claude-sonnet-4-6', backend: 'claude' }
    const wrapper = mount(SwitchRelayDialog, {
      props: { bot: bot0 as never, visible: true },
      global: { plugins: [ElementPlus, i18n] },
      attachTo: document.body,
    })
    await flushPromises()
    expect(document.querySelector('[data-test="switch-confirm"]')!.hasAttribute('disabled')).toBe(true)
    await (wrapper.vm as unknown as { confirm: () => Promise<void> }).confirm()
    await flushPromises()
    expect(bots.switchRelay).not.toHaveBeenCalled()
    // 换一台就恢复正常。
    await (wrapper.vm as unknown as { pick: (id: string) => Promise<void> }).pick('r2')
    await flushPromises()
    expect(document.querySelector('[data-test="switch-confirm"]')!.hasAttribute('disabled')).toBe(true)
    await (wrapper.vm as unknown as { checkTarget: () => Promise<void> }).checkTarget()
    await flushPromises()
    expect(document.querySelector('[data-test="switch-confirm"]')!.hasAttribute('disabled')).toBe(false)
    await (wrapper.vm as unknown as { confirm: () => Promise<void> }).confirm()
    await flushPromises()
    expect(bots.switchRelay).toHaveBeenCalledWith('b1', { relay_server_id: 'r2', model: 'codex/gpt-5.5', workspace_mode: 'copy', target_directory: '/home/ai/project', allow_stored_memory: false }, 1)
    wrapper.unmount()
  })

  it('picks target default model, warns on backend change and submits with version', async () => {
    const bot = { id: 'b1', version: 1, relay_server_id: 'r1', model: 'vllm/claude-sonnet-4-6', backend: 'claude' }
    const wrapper = mount(SwitchRelayDialog, {
      props: { bot: bot as never, visible: true },
      global: { plugins: [ElementPlus, i18n] },
      attachTo: document.body,
    })
    await flushPromises()
    await (wrapper.vm as unknown as { pick: (id: string) => Promise<void> }).pick('r2')
    await flushPromises()
    expect(document.body.textContent).toContain(i18n.global.t('bots.switch.backendChange'))
    await (wrapper.vm as unknown as { checkTarget: () => Promise<void> }).checkTarget()
    await (wrapper.vm as unknown as { confirm: () => Promise<void> }).confirm()
    await flushPromises()
    expect(bots.switchRelay).toHaveBeenCalledWith('b1', { relay_server_id: 'r2', model: 'codex/gpt-5.5', workspace_mode: 'copy', target_directory: '/home/ai/project', allow_stored_memory: false }, 1)
    expect(wrapper.emitted('switched')).toBeTruthy()
    wrapper.unmount()
  })

  // 换机的 409 不全是并发冲突（例如目标工作目录已被占用）：只有版本冲突才提示刷新，并换上最新版本号。
  it('reloads the version after a version conflict and shows other 409s verbatim', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(bots.switchRelay).mockClear()
    vi.mocked(bots.switchRelay)
      .mockRejectedValueOnce(new ApiError(409, VERSION_CONFLICT_CODE, '机器人已被其他操作修改，请刷新后重试'))
      .mockRejectedValueOnce(new ApiError(409, 409, '该实例工作目录已属于另一个机器人'))
    const bot = { id: 'b1', version: 1, relay_server_id: 'r1', model: 'vllm/claude-sonnet-4-6', backend: 'claude' }
    const wrapper = mount(SwitchRelayDialog, {
      props: { bot: bot as never, visible: true },
      global: { plugins: [ElementPlus, i18n] },
      attachTo: document.body,
    })
    await flushPromises()
    const vm = wrapper.vm as unknown as { pick: (id: string) => Promise<void>; confirm: () => Promise<void> }
    await vm.pick('r2')
    await (wrapper.vm as unknown as { checkTarget: () => Promise<void> }).checkTarget()
    await flushPromises()
    await vm.confirm()
    await flushPromises()
    expect(warning).toHaveBeenCalledWith(i18n.global.t('common.conflictReloaded'))
    expect(bots.get).toHaveBeenCalledWith('b1')
    await vm.confirm()
    await flushPromises()
    expect(error).toHaveBeenCalledWith('该实例工作目录已属于另一个机器人')
    expect(error).not.toHaveBeenCalledWith(i18n.global.t('common.conflict'))
    await vm.confirm()
    await flushPromises()
    expect(vi.mocked(bots.switchRelay).mock.calls.map((c) => c[2])).toEqual([1, 7, 7])
    expect(wrapper.emitted('switched')).toBeTruthy()
    warning.mockRestore()
    error.mockRestore()
    wrapper.unmount()
  })
  it('allows first runtime assignment without a source or stored memory snapshot', async () => {
    vi.mocked(workspace.preview).mockResolvedValueOnce({ directory: '/home/ai/project', exists: false, empty: true, owned: false, source_online: false, git_configured: false, memory_snapshot_at: null })
    vi.mocked(bots.switchRelay).mockClear()
    const wrapper = mount(SwitchRelayDialog, {
      props: { bot: { id: 'b1', version: 1, relay_server_id: null, model: 'vllm/claude-sonnet-4-6', backend: 'claude', working_dir: '/home/ai/project' } as never, visible: true },
      global: { plugins: [ElementPlus, i18n] }, attachTo: document.body,
    })
    await flushPromises()
    const vm = wrapper.vm as unknown as { pick: (id: string) => Promise<void>; checkTarget: () => Promise<void>; confirm: () => Promise<void> }
    await vm.pick('r2')
    await vm.checkTarget()
    await flushPromises()
    expect(document.querySelector('[data-test="switch-confirm"]')!.hasAttribute('disabled')).toBe(false)
    await vm.confirm()
    expect(bots.switchRelay).toHaveBeenCalledWith('b1', expect.objectContaining({ relay_server_id: 'r2', allow_stored_memory: false }), 1)
    wrapper.unmount()
  })

})
