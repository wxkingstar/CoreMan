import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  catalog: {
    list: vi.fn().mockResolvedValue([
      { provider: 'claude', model: 'vllm/claude-sonnet-4-6', display_name: 'Sonnet', is_default: true, retired: false, supports_xhigh: false, supports_max: false, sort_order: 100, backend: 'claude' },
      { provider: 'codex', model: 'codex/gpt-5.5', display_name: null, is_default: true, retired: false, supports_xhigh: false, supports_max: false, sort_order: 100, backend: 'codex' },
    ]),
    patch: vi.fn().mockImplementation(async (p: string, m: string, body: Record<string, unknown>) => ({ provider: p, model: m, ...body })),
    create: vi.fn(), remove: vi.fn(),
    replace: vi.fn().mockImplementation(async (body: Record<string, unknown>) => ({
      provider: 'codex', new: body.new, old: body.old, created: true,
      default_before: 'codex/gpt-5.5', default_after: body.new,
      bots: [{ id: 'b1', name: 'sales', model: 'codex/gpt-5.5', effort_level: 'max', new_effort_level: 'xhigh' }],
      relays: [],
    })),
  },
}))

import { catalog } from '@/api/admin'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import CatalogPanel from '@/views/CatalogPanel.vue'

describe('CatalogPanel', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('lists by provider and toggles retired', async () => {
    useAuthStore().user = { id: 'me', login_name: 'a', display_name: 'A', role: 'platform_admin', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    const wrapper = mount(CatalogPanel, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).toContain('vllm/claude-sonnet-4-6')
    expect(wrapper.text()).toContain('codex/gpt-5.5')
    await wrapper.get('[data-test="retired-vllm/claude-sonnet-4-6"]').trigger('click')
    await flushPromises()
    expect(catalog.patch).toHaveBeenCalledWith('claude', 'vllm/claude-sonnet-4-6', { retired: true })
    await wrapper.get('[data-test="max-codex/gpt-5.5"]').trigger('click')
    await flushPromises()
    expect(catalog.patch).toHaveBeenCalledWith('codex', 'codex/gpt-5.5', { supports_max: true })
  })

  it('previews a replacement before applying it', async () => {
    useAuthStore().user = { id: 'me', login_name: 'a', display_name: 'A', role: 'platform_admin', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: null }
    const wrapper = mount(CatalogPanel, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="replace-model-codex/gpt-5.5"]').trigger('click')
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('input[data-test="replace-new"], [data-test="replace-new"] input')!
    input.value = 'codex/gpt-6'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    const apply = document.querySelector<HTMLButtonElement>('[data-test="replace-apply"]')!
    expect(apply.disabled).toBe(true)
    document.querySelector<HTMLButtonElement>('[data-test="replace-preview"]')!.click()
    await flushPromises()
    expect(catalog.replace).toHaveBeenLastCalledWith(expect.objectContaining({ provider: 'codex', new: 'codex/gpt-6', old: ['codex/gpt-5.5'], dry_run: true }))
    expect(document.querySelector('[data-test="replace-plan"]')!.textContent).toContain('max → xhigh')
    expect(apply.disabled).toBe(false)
    apply.click()
    await flushPromises()
    expect(catalog.replace).toHaveBeenLastCalledWith(expect.objectContaining({ dry_run: false }))
    wrapper.unmount()
  })
})
