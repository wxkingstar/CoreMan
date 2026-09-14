import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'

// vi.mock() 的工厂会被提升到模块顶部，直接引用模块级 const 会 ReferenceError；
// vi.hoisted() 先于它执行，返回值可以安全地在工厂里用（同 BotsView.spec）。
const { relayRow } = vi.hoisted(() => ({
  relayRow: (id: string, provider: string, def: string) => ({
    id, name: id, host: 'h', clawrelay_port: 1, relay_url: 'http://h:1', model_provider: provider,
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
    switchRelay: vi.fn().mockResolvedValue({
      old_relay_id: 'r1', new_relay_id: 'r2',
      old_model: 'vllm/claude-sonnet-4-6', new_model: 'codex/gpt-5.5',
      bot: { id: 'b1', version: 2 },
    }),
  },
}))

import { bots } from '@/api/admin'
import { i18n } from '@/i18n'
import SwitchRelayDialog from '@/views/SwitchRelayDialog.vue'

describe('SwitchRelayDialog', () => {
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
    expect(document.querySelector('[data-test="switch-confirm"]')!.hasAttribute('disabled')).toBe(false)
    await (wrapper.vm as unknown as { confirm: () => Promise<void> }).confirm()
    await flushPromises()
    expect(bots.switchRelay).toHaveBeenCalledWith('b1', { relay_server_id: 'r2', model: 'codex/gpt-5.5' }, 1)
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
    await (wrapper.vm as unknown as { confirm: () => Promise<void> }).confirm()
    await flushPromises()
    expect(bots.switchRelay).toHaveBeenCalledWith('b1', { relay_server_id: 'r2', model: 'codex/gpt-5.5' }, 1)
    expect(wrapper.emitted('switched')).toBeTruthy()
    wrapper.unmount()
  })
})
