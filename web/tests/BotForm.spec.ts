import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,qr') } }))
vi.mock('@/api/feishuApps', () => ({
  feishuApps: { startRegistration: vi.fn(), registration: vi.fn(), cancelRegistration: vi.fn() },
}))
vi.mock('@/api/admin', () => ({
  bots: { create: vi.fn().mockImplementation(async (b: Record<string, unknown>) => ({ id: 'b1', version: 1, ...b })), validate: vi.fn().mockResolvedValue({ valid: true }), patch: vi.fn(), get: vi.fn() },
  relays: { list: vi.fn().mockResolvedValue({ items: [{ id: 'r1', name: 'claude01', unavailable_reason: null, effective_models: ['vllm/claude-sonnet-4-6'], model_provider: 'claude', team_id: null, team_name: null, is_active: true, default_model: 'vllm/claude-sonnet-4-6' }, { id: 'r2', name: 'codex01', unavailable_reason: null, effective_models: ['codex/gpt-5.5'], model_provider: 'codex', team_id: null, team_name: null, is_active: true, default_model: 'codex/gpt-5.5' }], total: 2, page: 1, per_page: 200 }), models: vi.fn().mockImplementation(async (id: string) => id === 'r1' ? { provider: 'claude', mode: 'inherit', models: ['vllm/claude-sonnet-4-6', 'vllm/claude-opus-4-6'], default: 'vllm/claude-sonnet-4-6' } : { provider: 'codex', mode: 'inherit', models: ['codex/gpt-5.5'], default: 'codex/gpt-5.5' }) },
  catalog: { list: vi.fn().mockResolvedValue([{ provider: 'claude', model: 'vllm/claude-sonnet-4-6', supports_xhigh: false, retired: false, is_default: true, display_name: null, sort_order: 1, backend: 'claude' }, { provider: 'codex', model: 'codex/gpt-5.5', supports_xhigh: true, retired: false, is_default: true, display_name: null, sort_order: 1, backend: 'codex' }]) },
  settings: { defaults: vi.fn().mockResolvedValue({ default_model: 'vllm/claude-sonnet-4-6', default_verbosity_level: 2, default_effort_level: 'high' }) },
  teams: { list: vi.fn().mockResolvedValue([]) },
}))

import { bots, relays, settings } from '@/api/admin'
import { feishuApps } from '@/api/feishuApps'
import { ApiError } from '@/api/client'
import FeishuRegistrationDialog from '@/components/feishuApp/FeishuRegistrationDialog.vue'
import { VERSION_CONFLICT_CODE } from '@/utils/errors'
import type { BotOut } from '@/api/types'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import BotForm from '@/views/BotForm.vue'

// 完整的 BotOut（含敏感字段），用于编辑态。除 team_id 外每个字段都与表单载入后的值一致，
// 这样 changedFields() 的产出里只可能出现 team_id。
const editBot: BotOut = {
  id: 'b1', bot_key: 'sales_bot', platform: 'wecom', name: '销售助手', description: '',
  avatar_url: null, enabled: true, team_id: 't1', team_name: '销售',
  created_by: 'me', created_by_name: 'U',
  relay_server_id: 'r1', relay_name: 'claude01', relay_url: 'http://h:1',
  model: 'vllm/claude-sonnet-4-6', backend: 'claude', working_dir: '/home/ai/sales_bot',
  verbosity_level: 1, effort_level: null, sse_timeout_seconds: 3600,
  welcome_message: null, notify_webhook_url: 'ht••••-1',
  member_count: 0, allowed_user_count: 0,
  permissions: {
    role: 'creator', can_view_sensitive: true, can_view_env_full: false, can_edit: true,
    can_switch_relay: true, can_toggle: true, can_delete: true, can_manage_members: true,
    can_reassign_team: true,
  },
  version: 1, created_at: '', updated_at: '',
  system_prompt: '你是销售助手', credentials: { bot_id: 'bot-1', secret: '••••••••' },
  env_vars: { FOO: '••••••••' },
}

describe('BotForm', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('keeps the runtime selected but selects no backend when none are ready', async () => {
    vi.mocked(relays.list).mockResolvedValueOnce({ items: [
      { id: 'offline-ai', name: 'demo-node / codex', runtime_node_id: 'demo-node', runtime_name: 'demo-node', model_provider: 'codex', unavailable_reason: 'login_required', effective_models: ['codex/gpt-5.5'] },
    ] } as never)
    const wrapper = mount(BotForm, { props: { mode: 'create' }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { selectRuntime: (id: string) => Promise<void>; selectRelay: (id: string) => Promise<void>; selectedRuntime: string; form: { relay_server_id: string | null } }
    await vm.selectRuntime('demo-node')
    expect(vm.selectedRuntime).toBe('demo-node')
    expect(vm.form.relay_server_id).toBeNull()
    await vm.selectRelay('offline-ai')
    expect(vm.form.relay_server_id).toBeNull()
    const option = wrapper.findAllComponents({ name: 'ElOption' }).find(o => o.props('value') === 'offline-ai')!
    expect(option.props('disabled')).toBe(true)
    expect(option.props('label')).toContain('未登录')
    wrapper.unmount()
  })

  it('links working_dir to bot_key, switches model with relay, and submits create', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    const wrapper = mount(BotForm, { props: { mode: 'create' }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="agent_timeout"]').exists()).toBe(false)
    await wrapper.get('[data-test="bot_key"] input').setValue('sales_bot')
    expect((wrapper.get('[data-test="working_dir"] input').element as HTMLInputElement).value).toBe('/home/ai/sales_bot')
    expect((wrapper.vm as unknown as { form: { model: string; verbosity_level: number } }).form.model).toBe('vllm/claude-sonnet-4-6')
    ;(wrapper.vm as unknown as { selectRelay: (id: string) => Promise<void> }).selectRelay('r2')
    await flushPromises()
    expect((wrapper.vm as unknown as { form: { model: string } }).form.model).toBe('codex/gpt-5.5')
    await wrapper.get('[data-test="name"] input').setValue('销售')
    ;(wrapper.vm as unknown as { form: { platform: string } }).form.platform = 'wecom'
    await flushPromises()
    await wrapper.get('[data-test="cred-bot_id"] input').setValue('bot-id')
    await wrapper.get('[data-test="cred-secret"] input').setValue('secret-value')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(bots.create).toHaveBeenCalledWith(expect.objectContaining({ bot_key: 'sales_bot', relay_server_id: 'r2', model: 'codex/gpt-5.5', working_dir: '/home/ai/sales_bot', credentials: { bot_id: 'bot-id', secret: 'secret-value' } }))
    expect(wrapper.emitted('saved')).toBeTruthy()
  })

  it('patches team_id as an explicit null when a manager clears the team', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'platform_admin', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    vi.mocked(bots.patch).mockResolvedValue({ ...editBot, team_id: null, team_name: null, version: 2 })
    const wrapper = mount(BotForm, { props: { mode: 'edit', bot: editBot }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    // Element Plus 的清空手势派发的是 undefined 而不是 null，这里走模板上那个处理器把它归一化。
    const teamSelect = wrapper.get('[data-test="team"]').findComponent({ name: 'ElSelect' })
    teamSelect.vm.$emit('update:modelValue', undefined)
    await flushPromises()
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(bots.patch).toHaveBeenCalledWith('b1', { team_id: null }, 1)
    // 关键点：axios 会 JSON.stringify 请求体，undefined 会让这个键整个消失。
    const body = vi.mocked(bots.patch).mock.calls[0][1]
    expect(Object.keys(JSON.parse(JSON.stringify(body)) as Record<string, unknown>)).toEqual(['team_id'])
  })

  it('keeps notification destinations out of the employee editor', async () => {
    const wrapper = mount(BotForm, { props: { mode: 'edit', bot: editBot }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    expect(wrapper.find('[data-test="webhook"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="command_modules"]').exists()).toBe(false)
    wrapper.unmount()
  })

  // 机器人绑在自己看不见的 relay 上（visibility=admins）时 /models 是 404：静默回落到目录全集。
  it('falls back to the catalog when the relay model list is not visible (404)', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(relays.models).mockRejectedValueOnce(new ApiError(404, 404, 'relay 实例不存在'))
    const wrapper = mount(BotForm, {
      props: { mode: 'edit', bot: { ...editBot, relay_url: null } },
      global: { plugins: [ElementPlus, i18n] },
    })
    await flushPromises()
    expect(error).not.toHaveBeenCalled()
    const vm = wrapper.vm as unknown as { modelOptions: string[]; form: { model: string } }
    expect(vm.modelOptions).toEqual(['vllm/claude-sonnet-4-6', 'codex/gpt-5.5'])
    expect(vm.form.model).toBe('vllm/claude-sonnet-4-6')
    error.mockRestore()
  })

  // 422 明细要落到对应表单项上，提示里用表单标签而不是字段名。
  it('shows backend validation errors on the matching form item', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(bots.create).mockRejectedValueOnce(new ApiError(422, 422, '参数校验失败', [
      { loc: ['body', 'working_dir'], msg: 'Value error, 工作目录必须是绝对路径', type: 'value_error' },
    ]))
    const wrapper = mount(BotForm, { props: { mode: 'create' }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    await wrapper.get('[data-test="bot_key"] input').setValue('sales_bot')
    await wrapper.get('[data-test="name"] input').setValue('销售')
    await (wrapper.vm as unknown as { selectRelay: (id: string) => Promise<void> }).selectRelay('r1')
    ;(wrapper.vm as unknown as { form: { platform: string } }).form.platform = 'wecom'
    await flushPromises()
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(error).toHaveBeenCalledWith(`${i18n.global.t('bots.workingDir')}：工作目录必须是绝对路径`)
    // 错误文字在 transition 里异步出现，这里断言表单项已进入错误态。
    expect(wrapper.get('[data-test="working_dir"]').classes()).toContain('is-error')
    expect(wrapper.get('[data-test="name"]').classes()).not.toContain('is-error')
    // 出错字段收在「更多设置」里：自动展开，免得用户找不到。
    expect((wrapper.vm as unknown as { moreOpen: string[] }).moreOpen).toEqual(['more'])
    expect(wrapper.emitted('saved')).toBeFalsy()
    error.mockRestore()
    wrapper.unmount()
  })

  // 只有版本冲突才提示「已被他人修改」；工作目录被占用这类 409 要把后端原话给用户，不能误导去刷新。
  it('shows the backend message for non-version 409 conflicts', async () => {
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    vi.mocked(bots.patch).mockRejectedValueOnce(new ApiError(409, 409, '该实例工作目录已属于另一个机器人'))
    const wrapper = mount(BotForm, { props: { mode: 'edit', bot: editBot }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    await wrapper.get('[data-test="working_dir"] input').setValue('/data/skills/other_bot')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(error).toHaveBeenCalledWith('该实例工作目录已属于另一个机器人')
    expect(warning).not.toHaveBeenCalled()
    expect(bots.get).not.toHaveBeenCalled()
    error.mockRestore()
    warning.mockRestore()
    wrapper.unmount()
  })

  // 版本冲突后要拿到最新版本号，否则用户再点保存还是带旧 If-Match，必然再 409。
  it('reloads the version after an optimistic-lock conflict and retries with it', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    vi.mocked(bots.patch).mockReset()
    vi.mocked(bots.patch)
      .mockRejectedValueOnce(new ApiError(409, VERSION_CONFLICT_CODE, '记录已被他人修改，请刷新后重试'))
      .mockResolvedValueOnce({ ...editBot, name: '新名字', version: 6 })
    vi.mocked(bots.get).mockResolvedValueOnce({ ...editBot, description: '别人改的', version: 5 })
    const wrapper = mount(BotForm, { props: { mode: 'edit', bot: editBot }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    await wrapper.get('[data-test="name"] input').setValue('新名字')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(warning).toHaveBeenCalledWith(i18n.global.t('common.conflictReloaded'))
    expect(bots.get).toHaveBeenCalledWith('b1')
    expect(wrapper.emitted('saved')).toBeFalsy()
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    // 只提交自己改过的字段（别人改的 description 不会被旧值覆盖），版本号用刚拉到的最新值。
    expect(vi.mocked(bots.patch).mock.calls.map((c) => [c[1], c[2]])).toEqual([[{ name: '新名字' }, 1], [{ name: '新名字' }, 5]])
    expect(wrapper.emitted('saved')).toBeTruthy()
    warning.mockRestore()
    wrapper.unmount()
  })

  it('defaults new employees to Feishu with essentials visible and the rest collapsed', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    const wrapper = mount(BotForm, { props: { mode: 'create' }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { platform: string }; moreOpen: string[] }
    expect(vm.form.platform).toBe('feishu')
    expect(vm.moreOpen).toEqual([])
    expect(wrapper.find('.el-collapse-item.is-active').exists()).toBe(false)
    expect(wrapper.find('[data-test="feishu-one-click"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="cred-app_id"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="submit"]').text()).toBe(i18n.global.t('feishuApp.scanAndCreate'))
    // 运行时是必填项：没选时不发起校验、不弹扫码。
    await wrapper.get('[data-test="bot_key"] input').setValue('sales_bot')
    await wrapper.get('[data-test="name"] input').setValue('销售')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-test="relay"]').classes()).toContain('is-error')
    expect(bots.validate).not.toHaveBeenCalled()
    expect(vm.moreOpen).toEqual([])
    error.mockRestore()
    wrapper.unmount()
  })

  it('validates first, then creates the employee from the scanned registration', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    vi.mocked(feishuApps.startRegistration).mockResolvedValue({ id: 'reg1', purpose: 'create', status: 'pending', bot_id: null, app_id: null, url: 'https://open.feishu.cn/page/launcher?user_code=A', expires_at: '', retry_after: 30, error: null, created_at: null, reused: false })
    vi.mocked(bots.create).mockClear()
    const wrapper = mount(BotForm, { props: { mode: 'create' }, global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="bot_key"] input').setValue('sales_bot')
    await wrapper.get('[data-test="name"] input').setValue('销售')
    await (wrapper.vm as unknown as { selectRelay: (id: string) => Promise<void> }).selectRelay('r1')
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(bots.validate).toHaveBeenCalledWith(expect.objectContaining({ bot_key: 'sales_bot', platform: 'feishu', credentials: {} }))
    expect(bots.create).not.toHaveBeenCalled()
    const dialog = wrapper.findComponent(FeishuRegistrationDialog)
    expect((dialog.props() as { visible: boolean }).visible).toBe(true)
    expect(feishuApps.startRegistration).toHaveBeenCalledWith(expect.objectContaining({ purpose: 'create', name: '销售', reuse: true }))
    dialog.vm.$emit('succeeded', { id: 'reg1', purpose: 'create', status: 'succeeded', app_id: 'cli_1' })
    await flushPromises()
    expect(bots.create).toHaveBeenCalledWith(expect.objectContaining({ bot_key: 'sales_bot', credentials: {}, feishu_registration_id: 'reg1' }))
    expect(wrapper.emitted('saved')).toBeTruthy()
    wrapper.unmount()
  })

  // 模型目录为空或全部退役时默认模型是 null：保持空，由必填校验提示用户选择。
  it('keeps the model empty when there is no default model', async () => {
    useAuthStore().user = { id: 'me', login_name: 'u', display_name: 'U', role: 'member', locale: 'zh', email: null, avatar_url: null, source: 'sync', team_id: 't1' }
    vi.mocked(settings.defaults).mockResolvedValueOnce({ default_model: null, default_verbosity_level: 3, default_effort_level: null })
    const wrapper = mount(BotForm, { props: { mode: 'create' }, global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { model: string; verbosity_level: number } }
    expect(vm.form.model).toBe('')
    expect(vm.form.verbosity_level).toBe(3)
    wrapper.unmount()
  })
})
