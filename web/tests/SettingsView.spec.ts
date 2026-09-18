import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { serverSettings } = vi.hoisted(() => ({
  serverSettings: {
    bootstrap_admin_enabled: true, default_model: 'vllm/claude-sonnet-4-6', default_verbosity_level: 1,
    default_effort_level: null, session_ttl_hours: 72, jwt_issuer: 'coreman',
    max_concurrent_tasks: 30, fast_lane_slots: 2, card_icon_url: 'https://cdn.example.com/i.png', wecom_qr_provisioning_enabled: true,
    prompt_security_policy: '安全策略', prompt_codex_contract: 'codex 契约', prompt_runtime_mode: '运行模式',
    prompt_cron_mode: '定时约束', prompt_runtime_tail: '尾部提醒', prompt_verbosity_2: '简洁', prompt_verbosity_3: '标准', prompt_verbosity_4: '详细',
  },
}))

vi.mock('@/api/admin', () => ({
  settings: { get: vi.fn().mockResolvedValue({ ...serverSettings }), update: vi.fn().mockImplementation(async (b: Record<string, unknown>) => ({ ...serverSettings, ...b })) },
  catalog: { list: vi.fn().mockResolvedValue([{ provider: 'claude', model: 'vllm/claude-sonnet-4-6', retired: false, is_default: true, display_name: null, supports_xhigh: false, sort_order: 1, backend: 'claude' }]) },
}))

vi.mock('@/components/AlertSettings.vue', () => ({ default: { template: '<div />' } }))

vi.mock('@/components/NotificationSettings.vue', () => ({ default: { template: '<div />' } }))

import { settings } from '@/api/admin'
import { i18n } from '@/i18n'
import SettingsView from '@/views/SettingsView.vue'

describe('SettingsView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('submits only changed keys', async () => {
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="agent-timeout"]').exists()).toBe(false)
    ;(wrapper.vm as unknown as { form: { session_ttl_hours: number } }).form.session_ttl_hours = 24
    await wrapper.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(settings.update).toHaveBeenCalledWith({ session_ttl_hours: 24 })
  })

  it('does not call the API when nothing changed', async () => {
    vi.mocked(settings.update).mockClear()
    const info = vi.spyOn(ElMessage, 'info').mockReturnValue({ close: () => {} })
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    await wrapper.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(settings.update).not.toHaveBeenCalled()
    expect(info).toHaveBeenCalledWith(i18n.global.t('settings.nothingChanged'))
    info.mockRestore()
  })

  // 422（模型不在目录中 / 关引导登录前先配登录应用）由后端给中文原文，前端只负责显示。
  it('surfaces the backend message on failure', async () => {
    vi.mocked(settings.update).mockClear()
    vi.mocked(settings.update).mockRejectedValueOnce(new Error('请先配置并启用一个具备登录能力的平台应用，再关闭引导登录'))
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    ;(wrapper.vm as unknown as { form: { bootstrap_admin_enabled: boolean } }).form.bootstrap_admin_enabled = false
    await wrapper.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(settings.update).toHaveBeenCalledWith({ bootstrap_admin_enabled: false })
    expect(error).toHaveBeenCalledWith('请先配置并启用一个具备登录能力的平台应用，再关闭引导登录')
    error.mockRestore()
  })

  // 卡片来源图标：后端允许留空（不显示图标），所以清空必须真的提交 '' 而不是被当成「没改」。
  it('shows the card icon url and submits the cleared value', async () => {
    vi.mocked(settings.update).mockClear()
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect((wrapper.get('[data-test="card-icon-url"] input').element as HTMLInputElement).value).toBe('https://cdn.example.com/i.png')
    ;(wrapper.vm as unknown as { form: { card_icon_url: string } }).form.card_icon_url = ''
    await wrapper.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(settings.update).toHaveBeenCalledWith({ card_icon_url: '' })
  })

  it('submits changed prompt segment and concurrency', async () => {
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { prompt_runtime_tail: string; fast_lane_slots: number } }
    vm.form.prompt_runtime_tail = '新的尾部提醒'
    vm.form.fast_lane_slots = 3
    await wrapper.get('[data-test="save"]').trigger('click')
    await flushPromises()
    expect(settings.update).toHaveBeenCalledWith({ prompt_runtime_tail: '新的尾部提醒', fast_lane_slots: 3 })
  })
})

it('saves one section while retaining unsaved changes in another', async () => {
  setActivePinia(createPinia())
  vi.mocked(settings.update).mockClear()
  const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  const form = (wrapper.vm as unknown as { form: { session_ttl_hours: number; prompt_runtime_tail: string } }).form
  form.session_ttl_hours = 24
  form.prompt_runtime_tail = 'unsaved prompt'
  await wrapper.get('[data-test="save-runtime"]').trigger('click')
  await flushPromises()
  expect(settings.update).toHaveBeenLastCalledWith({ session_ttl_hours: 24 })
  expect(form.prompt_runtime_tail).toBe('unsaved prompt')
  wrapper.unmount()
})

// 默认模型由模型目录派生：设置页只读展示，PUT 不能带 default_model（后端 422）。
it('shows the default model read-only and never submits it', async () => {
  setActivePinia(createPinia())
  vi.mocked(settings.update).mockClear()
  const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  const item = wrapper.get('[data-test="default-model"]')
  expect(item.text()).toContain('vllm/claude-sonnet-4-6')
  expect(item.text()).toContain(i18n.global.t('settings.defaultModelHint'))
  expect(item.find('input').exists()).toBe(false)
  const form = (wrapper.vm as unknown as { form: { default_model: string | null; session_ttl_hours: number } }).form
  form.default_model = 'someone/else'
  form.session_ttl_hours = 12
  await wrapper.get('[data-test="save"]').trigger('click')
  await flushPromises()
  expect(settings.update).toHaveBeenCalledWith({ session_ttl_hours: 12 })
  wrapper.unmount()
})

it('explains a missing default model', async () => {
  setActivePinia(createPinia())
  vi.mocked(settings.get).mockResolvedValueOnce({ ...serverSettings, default_model: null } as never)
  const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus, i18n] } })
  await flushPromises()
  expect(wrapper.get('[data-test="default-model"]').text()).toContain(i18n.global.t('settings.defaultModelNone'))
  wrapper.unmount()
})
