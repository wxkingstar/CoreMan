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
  systems: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), grants: vi.fn(), saveGrants: vi.fn() },
  credentials: { clients: vi.fn(), keys: vi.fn(), create: vi.fn(), update: vi.fn(), rotate: vi.fn(), rotateKey: vi.fn() },
}))
const erp = { key: 'erp', name: 'ERP', description: '', base_url: 'https://erp.example', sitemap_url: '', enabled: true, sort_order: 0, default_for_all_bots: false, allowed_bot_ids: null, version: 1 }
const page = { page: 1, per_page: 50, total: 1, items: [erp] }
const plugins = [ElementPlus, i18n]
describe('Infrastructure management', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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
    await wrapper.get('button').trigger('click'); await flushPromises()
    const vm = wrapper.vm as unknown as { options: typeof erp[]; keys: string[]; save: () => Promise<void> }
    expect(vm.options.map(s => s.key)).toEqual(['erp'])
    vm.keys = []; await vm.save()
    expect(systems.saveGrants).toHaveBeenCalledWith('bot1', [], 7)
    expect(wrapper.emitted('saved')).toHaveLength(1)
    wrapper.unmount()
  })
})
