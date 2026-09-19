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
})
