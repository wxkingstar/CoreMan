import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// vi.mock() factories are hoisted above module-scope consts, so a plain top-level
// `const mockApp = {...}` would throw "Cannot access before initialization" once
// referenced below. vi.hoisted() runs first and makes its return value safe to use
// inside the factory.
const { mockApp } = vi.hoisted(() => ({
  mockApp: { id: 'p1', platform: 'wecom', name: '通讯录', capabilities: ['contact_sync', 'login'], corp_id: 'ww1', app_id: '1000002',
    secret: 'su••••ue', callback_token: null, callback_aes_key: null, extra: {}, enabled: true, version: 3, created_at: '', updated_at: '' },
}))

vi.mock('@/api/admin', () => ({
  platformApps: {
    list: vi.fn().mockResolvedValue({ items: [mockApp], total: 1, page: 1, per_page: 50 }),
    runs: vi.fn().mockResolvedValue([]),
    update: vi.fn().mockImplementation(async (_id: string, body: Record<string, unknown>, version: number) => ({ ...mockApp, ...body, version: version + 1 })),
    sync: vi.fn().mockResolvedValue({ run_id: 9 }),
    test: vi.fn().mockResolvedValue({ ok: true, message: 'ok' }),
    create: vi.fn(), remove: vi.fn(), get: vi.fn(),
  },
  syncRuns: { get: vi.fn().mockResolvedValue({ id: 9, status: 'success', started_at: '', finished_at: '', stats: { created: 1 }, error: null, triggered_by: null }) },
}))

import { platformApps, syncRuns } from '@/api/admin'
import { i18n } from '@/i18n'
import PlatformAppsView from '@/views/PlatformAppsView.vue'

describe('PlatformAppsView', () => {
  beforeEach(() => setActivePinia(createPinia()))
  afterEach(() => {
    vi.useRealTimers()
  })

  it('lists apps, toggles enabled with version and runs sync polling', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const wrapper = mount(PlatformAppsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.text()).toContain('通讯录')
    await wrapper.get('[data-test="toggle-p1"]').trigger('click')
    await flushPromises()
    expect(platformApps.update).toHaveBeenCalledWith('p1', expect.objectContaining({ enabled: false, secret: 'su••••ue' }), 3)
    await wrapper.get('[data-test="sync-p1"]').trigger('click')
    await flushPromises()
    expect(platformApps.sync).toHaveBeenCalledWith('p1')
    await vi.advanceTimersByTimeAsync(2100)
    expect(syncRuns.get).toHaveBeenCalledWith(9)
  })
})
