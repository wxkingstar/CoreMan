import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { i18n } from '@/i18n'
import SystemsView from '@/views/SystemsView.vue'
import CredentialsView from '@/views/CredentialsView.vue'
import SystemGrants from '@/components/SystemGrants.vue'
import { credentials, systems } from '@/api/infrastructure'

vi.mock('@/api/admin', () => ({ bots: { list: vi.fn().mockResolvedValue({ items: [], total: 0 }) } }))
vi.mock('@/api/infrastructure', () => ({
  systems: { providers: vi.fn(), list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), grants: vi.fn(), saveGrants: vi.fn() },
  credentials: { clients: vi.fn(), keys: vi.fn(), create: vi.fn(), update: vi.fn(), rotate: vi.fn(), rotateKey: vi.fn() },
}))
const erp = { key: 'erp', name: 'ERP', description: '', base_url: 'https://erp.example', sitemap_url: '', enabled: true, sort_order: 0, default_for_all_bots: false, allowed_bot_ids: null, version: 1 }
const page = { page: 1, per_page: 50, total: 1, items: [erp] }
const plugins = [ElementPlus, i18n]
describe('Infrastructure management', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(systems.providers).mockResolvedValue([{ id: 'builtin', max_token_ttl_seconds: null }, { id: 'issuer', max_token_ttl_seconds: 120 }])
    vi.mocked(systems.list).mockResolvedValue(page)
    vi.mocked(credentials.clients).mockResolvedValue({ items: [], total: 0, page: 1, per_page: 50 })
    vi.mocked(credentials.keys).mockResolvedValue([])
  })
  it('preserves an intentionally empty bot whitelist when creating a system', async () => {
    vi.mocked(systems.create).mockResolvedValue(erp)
    const wrapper = mount(SystemsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    expect(wrapper.text()).toContain('ERP')
    await wrapper.get('[data-test="create-system"]').trigger('click'); await flushPromises()
    const vm = wrapper.vm as unknown as { form: typeof erp; restricted: boolean }
    vm.form.key = 'new'; vm.form.name = 'New'; vm.restricted = true
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-test="save-system"]')!.click(); await flushPromises()
    expect(systems.create).toHaveBeenCalledWith(expect.objectContaining({ key: 'new', allowed_bot_ids: [] }))
    wrapper.unmount()
  })
  it('defaults new systems to an empty allowlist and flags systems open to all employees', async () => {
    vi.mocked(systems.create).mockResolvedValue(erp)
    const wrapper = mount(SystemsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    expect(wrapper.find('[data-test="open-to-all"]').text()).toBe(i18n.global.t('infra.openToAllBots'))
    await wrapper.get('[data-test="create-system"]').trigger('click'); await flushPromises()
    const vm = wrapper.vm as unknown as { form: typeof erp; restricted: boolean }
    expect(vm.restricted).toBe(true)
    expect(document.querySelector('[data-test="open-to-all-warning"]')).toBeNull()
    vm.form.key = 'oa'; vm.form.name = 'OA'
    document.querySelector<HTMLButtonElement>('[data-test="save-system"]')!.click(); await flushPromises()
    expect(systems.create).toHaveBeenCalledWith(expect.objectContaining({ key: 'oa', allowed_bot_ids: [] }))
    await wrapper.get('[data-test="create-system"]').trigger('click'); await flushPromises()
    vm.restricted = false; await flushPromises()
    expect(document.querySelector('[data-test="open-to-all-warning"]')?.textContent).toBe(i18n.global.t('infra.openToAllWarning'))
    wrapper.unmount()
  })
  it('loads safe provider choices and saves provider, audience and protected test URL', async () => {
    vi.mocked(systems.create).mockResolvedValue(erp)
    const wrapper = mount(SystemsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="create-system"]').trigger('click'); await flushPromises()
    expect(systems.providers).toHaveBeenCalled()
    expect(document.querySelector('[data-test="token-provider"]')).not.toBeNull()
    const vm = wrapper.vm as unknown as { form: { key: string; name: string; token_provider: string; token_audience: string; access_test_url: string } }
    vm.form.key = 'new'; vm.form.name = 'New'; vm.form.token_provider = 'issuer'
    vm.form.token_audience = 'erp-api'; vm.form.access_test_url = 'https://erp.example/api/me'
    document.querySelector<HTMLButtonElement>('[data-test="save-system"]')!.click(); await flushPromises()
    expect(systems.create).toHaveBeenCalledWith(expect.objectContaining({ token_provider: 'issuer', token_audience: 'erp-api', access_test_url: 'https://erp.example/api/me' }))
    wrapper.unmount()
  })
  it('preserves a deployment-removed provider while editing an existing system', async () => {
    const external = { ...erp, token_provider: 'removed', token_audience: 'erp-api', access_test_url: 'https://erp.example/api/me' }
    vi.mocked(systems.update).mockResolvedValue(external)
    const wrapper = mount(SystemsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    const vm = wrapper.vm as unknown as { edit: (row: typeof external) => Promise<void>; save: () => Promise<void>; form: typeof external; providerOptions: { id: string }[] }
    await vm.edit(external); await flushPromises()
    expect(vm.providerOptions.map(provider => provider.id)).toContain('removed')
    vm.form.name = 'Renamed'
    await vm.save(); await flushPromises()
    expect(systems.update).toHaveBeenCalledWith(external, expect.objectContaining({ name: 'Renamed', token_provider: 'removed', token_audience: 'erp-api', access_test_url: 'https://erp.example/api/me' }))
    wrapper.unmount()
  })
  it('shows the integration spec next to the OpenAPI URL and copies its public link', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, text: () => Promise.resolve('# 接入规范\n\n每个操作都要有 `operationId`。') })
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const wrapper = mount(SystemsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="create-system"]').trigger('click'); await flushPromises()
    expect(document.body.textContent).toContain(i18n.global.t('infra.openapiUrl'))
    document.querySelector<HTMLButtonElement>('[data-test="open-contract"]')!.click(); await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith('/integration/business-system-openapi-contract.md')
    expect(document.querySelector('[data-test="contract-content"]')?.textContent).toContain('operationId')
    document.querySelector<HTMLButtonElement>('[data-test="copy-contract-link"]')!.click(); await flushPromises()
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/integration/business-system-openapi-contract.md`)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
  it('shows a new client secret once and clears it when the dialog closes', async () => {
    vi.mocked(credentials.create).mockResolvedValue({ app_key: 'client', name: 'Client', scopes: ['org'], enabled: true, version: 1, last_used_at: null, has_secret: true, secret: 'synthetic-one-time-secret' })
    const wrapper = mount(CredentialsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="create-client"]').trigger('click'); await flushPromises()
    const vm = wrapper.vm as unknown as { form: { app_key: string; name: string; scopes: string[] }; secret: string }
    vm.form.app_key = 'client'; vm.form.name = 'Client'; vm.form.scopes = ['org']
    document.querySelector<HTMLButtonElement>('[data-test="save-client"]')!.click(); await flushPromises()
    expect(vm.secret).toBe('synthetic-one-time-secret')
    const secretDialog = wrapper.findAllComponents({ name: 'ElDialog' }).find(d => d.props('title') === i18n.global.t('infra.secretOnce'))!
    secretDialog.vm.$emit('close'); await flushPromises()
    expect(vm.secret).toBe('')
    wrapper.unmount()
  })
  it('explains invalid client identifiers and allows correction before saving', async () => {
    vi.mocked(credentials.create).mockResolvedValue({ app_key: 'test-client', name: '测试', scopes: ['push'], enabled: true, version: 1, last_used_at: null, has_secret: true, secret: 'test-secret' })
    const wrapper = mount(CredentialsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="create-client"]').trigger('click'); await flushPromises()
    const vm = wrapper.vm as unknown as { form: { app_key: string; name: string; scopes: string[] }; save: () => Promise<void> }
    vm.form.app_key = '测试'; vm.form.name = '测试'; vm.form.scopes = ['push']
    await vm.save(); await flushPromises()
    expect(credentials.create).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain(i18n.global.t('infra.clientKeyHint'))
    vm.form.app_key = 'test-client'
    await vm.save(); await flushPromises()
    expect(credentials.create).toHaveBeenCalledWith({ app_key: 'test-client', name: '测试', scopes: ['push'], enabled: true })
    wrapper.unmount()
  })
  it('clears validation on reopening and rejects whitespace-only names', async () => {
    const wrapper = mount(CredentialsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { app_key: string; name: string }; save: () => Promise<void>; edit: (row: null) => Promise<void> }
    await vm.edit(null); await flushPromises()
    vm.form.app_key = 'valid-key'; vm.form.name = '   '
    await vm.save(); await flushPromises()
    expect(credentials.create).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(document.querySelectorAll('.el-form-item__error').length).toBeGreaterThan(0))
    await vm.edit(null); await flushPromises()
    await vi.waitFor(() => expect(document.querySelectorAll('.el-form-item__error')).toHaveLength(0))
    wrapper.unmount()
  })
  it('keeps retry visible on the key tab and hides misleading empty tables on failure', async () => {
    vi.mocked(credentials.keys).mockRejectedValueOnce(new Error('Key load failed'))
    const wrapper = mount(CredentialsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    const vm = wrapper.vm as unknown as { tab: string; load: () => Promise<void> }
    vm.tab = 'keys'; await flushPromises()
    expect(wrapper.text()).toContain('Key load failed')
    expect(wrapper.findComponent({ name: 'LoadState' }).isVisible()).toBe(true)
    expect(wrapper.findAllComponents({ name: 'ElTable' })).toHaveLength(0)
    vi.mocked(credentials.keys).mockResolvedValue([])
    await vm.load(); await flushPromises()
    expect(wrapper.text()).not.toContain('Key load failed')
    expect(wrapper.findAllComponents({ name: 'ElTable' })).toHaveLength(2)
    wrapper.unmount()
  })

  it('marks the deployment-configured signing key and explains rotation does not affect it', async () => {
    vi.mocked(credentials.keys).mockResolvedValue([
      { kid: 'legacy-2024', is_active: true, created_at: null, retired_at: null, external: true },
      { kid: 'platform', is_active: true, created_at: '2026-09-01T00:00:00Z', retired_at: null, external: false },
    ])
    const wrapper = mount(CredentialsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    const vm = wrapper.vm as unknown as { tab: string }
    vm.tab = 'keys'; await flushPromises()
    expect(wrapper.text()).toContain('部署配置')
    expect(wrapper.text()).toContain('BOT_JWT_*')
    expect(wrapper.text()).toContain('使用中')
    wrapper.unmount()
  })
  it('uses translated scope labels in the client list', async () => {
    vi.mocked(credentials.clients).mockResolvedValue({ items: [{ app_key: 'client', name: 'Client', scopes: ['org', 'push'], enabled: true, version: 1, last_used_at: null, has_secret: true }], total: 1, page: 1, per_page: 50 })
    const wrapper = mount(CredentialsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    expect(wrapper.find('.scope-tags').text()).toContain(i18n.global.t('infra.scopeNames.org'))
    expect(wrapper.find('.scope-tags').text()).toContain(i18n.global.t('infra.scopeNames.push'))
    wrapper.unmount()
  })
  it('offers no retired cron scope and drops it when editing an old client', async () => {
    const old = { app_key: 'old', name: 'Old', scopes: ['cron', 'org'], enabled: true, version: 3, last_used_at: null, has_secret: true }
    vi.mocked(credentials.clients).mockResolvedValue({ items: [old], total: 1, page: 1, per_page: 50 })
    vi.mocked(credentials.update).mockResolvedValue({ ...old, scopes: ['org'], version: 4 })
    const wrapper = mount(CredentialsView, { global: { plugins }, attachTo: document.body })
    await flushPromises()
    const vm = wrapper.vm as unknown as { scopes: string[]; form: { scopes: string[] }; edit: (row: typeof old) => Promise<void>; save: () => Promise<void> }
    expect(vm.scopes).not.toContain('cron')
    await vm.edit(old); await flushPromises()
    expect(vm.form.scopes).toEqual(['org'])
    await vm.save(); await flushPromises()
    expect(credentials.update).toHaveBeenCalledWith(expect.objectContaining({ app_key: 'old', scopes: ['org'] }))
    wrapper.unmount()
  })
  it('saves grants with the loaded version and excludes disallowed systems', async () => {
    vi.mocked(systems.list).mockResolvedValue({ ...page, total: 2, items: [erp, { ...erp, key: 'blocked', name: 'Blocked', allowed_bot_ids: [] }] })
    vi.mocked(systems.grants).mockResolvedValue({ system_keys: ['erp'], version: 7 })
    vi.mocked(systems.saveGrants).mockResolvedValue({ system_keys: [], version: 8 })
    const wrapper = mount(SystemGrants, { props: { botId: 'bot1' }, global: { plugins } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { options: typeof erp[]; keys: string[]; save: () => Promise<void> }
    expect(vm.options.map(s => s.key)).toEqual(['erp'])
    expect(wrapper.text()).toContain('（1）')
    vm.keys = []; await vm.save(); await flushPromises()
    expect(systems.saveGrants).toHaveBeenCalledWith('bot1', [], 7)
    expect(wrapper.emitted('saved')).toHaveLength(1)
    // 保存后页面上直接反映新状态：计数归零，保存按钮回到不可点。
    expect(wrapper.text()).toContain('（0）')
    expect(wrapper.get('[data-test="save-grants"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})
