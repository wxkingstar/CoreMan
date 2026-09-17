import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,qr') } }))
vi.mock('@/api/admin', () => ({
  departments: { tree: vi.fn().mockResolvedValue([]) },
  users: { list: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 }) },
}))
vi.mock('@/api/feishuApps', () => ({
  feishuApps: {
    overview: vi.fn(), updateBase: vi.fn(), uploadAvatar: vi.fn(), updateBot: vi.fn(), updateVisibility: vi.fn(),
    publish: vi.fn(), applyScopes: vi.fn(), createCommand: vi.fn(), updateCommand: vi.fn(), deleteCommand: vi.fn(), addDefaultCommands: vi.fn(),
    startRegistration: vi.fn(), registration: vi.fn(), cancelRegistration: vi.fn(),
  },
}))

import { feishuApps, type FeishuAppOverview } from '@/api/feishuApps'
import FeishuAppPanel from '@/components/feishuApp/FeishuAppPanel.vue'
import FeishuRegistrationDialog from '@/components/feishuApp/FeishuRegistrationDialog.vue'
import { i18n } from '@/i18n'

function overview(over: Partial<FeishuAppOverview> = {}): FeishuAppOverview {
  return {
    app_id: 'cli_1',
    app: { app_id: 'cli_1', name: '销售助手', description: '卖货', avatar_url: null, status: 1, create_source: null, primary_language: 'zh_cn', help_use: null, online_version_id: 'oav_1', under_review: false },
    scopes: [{ scope: 'im:message:send_as_bot', token_types: ['tenant'], level: 1, granted: true }],
    missing_scopes: { tenant: [], user: ['im:message.send_as_user'] },
    pending_grants: ['contact:user.employee_id:readonly'],
    personal_levels: { messages_readonly: true, all_except_send: true, all: false },
    personal_connections: 2,
    versions: {
      online: { version_id: 'oav_1', version: '1.0.2', status: 1, create_time: null, publish_time: '1758000000', remark: null, update_remark: null, visibility: { is_all: true, open_ids: [], department_ids: [] }, events: [], bot: { menu_enabled: true, menu_display_strategy: 1, menus: [{ menu_id: 'm1', parent_menu_id: null, name: '文档', kind: 'link', pc_url: 'https://doc', mobile_url: 'https://doc', event_key: null, sort: 0 }] } },
      under_review: null,
    },
    slash_commands: [{ command_id: '9', command: 'report', description: '日报', icon_key: 'skill_outlined', update_time: null }],
    errors: { app: null, grants: null, versions: null, slash_commands: null },
    origin: { one_click: true, created_by_name: '张三', created_at: '2026-09-17T00:00:00Z' },
    employee: { name: '销售助手 AI', description: '负责销售问答' },
    next_version: '1.0.3',
    console_url: 'https://open.feishu.cn/app/cli_1',
    ...over,
  }
}

function mountPanel() {
  return mount(FeishuAppPanel, { props: { botId: 'b1' }, global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
}

describe('FeishuAppPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
    vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
  })

  it('shows origin, connection tiers, missing scopes and pending approvals', async () => {
    vi.mocked(feishuApps.overview).mockResolvedValue(overview())
    const wrapper = mountPanel()
    await flushPromises()
    expect(feishuApps.overview).toHaveBeenCalledWith('b1')
    const text = wrapper.text()
    expect(text).toContain('cli_1')
    expect(text).toContain(i18n.global.t('feishuApp.originOneClick', { name: '张三', time: '' }).split(' ')[0])
    expect(wrapper.get('[data-test="level-all"]').text()).toContain('✗')
    expect(wrapper.get('[data-test="level-messages_readonly"]').text()).toContain('✓')
    expect(wrapper.get('[data-test="missing-scopes"]').text()).toContain('im:message.send_as_user')
    expect(wrapper.get('[data-test="pending-grants"]').text()).toContain('contact:user.employee_id:readonly')
    vi.mocked(feishuApps.applyScopes).mockResolvedValue({ ok: true })
    await wrapper.get('[data-test="apply-scopes"]').trigger('click')
    await flushPromises()
    expect(feishuApps.applyScopes).toHaveBeenCalledWith('b1')
    wrapper.unmount()
  })

  it('syncs employee profile, publishes the suggested next version and opens the update scan', async () => {
    vi.mocked(feishuApps.overview).mockResolvedValue(overview())
    vi.mocked(feishuApps.updateBase).mockResolvedValue({ ok: true, publish_required: true })
    vi.mocked(feishuApps.publish).mockResolvedValue({ version_id: 'oav_2', version: '1.0.3' })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-test="use-employee"]').trigger('click')
    await wrapper.get('[data-test="profile-save"]').trigger('click')
    await flushPromises()
    expect(feishuApps.updateBase).toHaveBeenCalledWith('b1', { language: 'zh_cn', name: '销售助手 AI', description: '负责销售问答', help_use: null })

    expect((wrapper.get('input[data-test="publish-version"]').element as HTMLInputElement).value).toBe('1.0.3')
    await wrapper.get('textarea[data-test="publish-changelog"]').setValue('更新名称')
    await wrapper.get('[data-test="publish-submit"]').trigger('click')
    await flushPromises()
    expect(feishuApps.publish).toHaveBeenCalledWith('b1', expect.objectContaining({ version: '1.0.3', changelog: '更新名称', remark: i18n.global.t('feishuApp.publishRemarkDefault') }))

    await wrapper.get('[data-test="feishu-app-update"]').trigger('click')
    await flushPromises()
    const dialog = wrapper.findComponent(FeishuRegistrationDialog)
    expect((dialog.props() as { visible: boolean; purpose: string; botId: string })).toMatchObject({ visible: true, purpose: 'update', botId: 'b1' })
    wrapper.unmount()
  })

  it('saves bot menus with their tree order and disables edits while under review', async () => {
    vi.mocked(feishuApps.overview).mockResolvedValue(overview())
    vi.mocked(feishuApps.updateBot).mockResolvedValue({ ok: true, publish_required: true })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-test="bot-save"]').trigger('click')
    await flushPromises()
    expect(feishuApps.updateBot).toHaveBeenCalledWith('b1', expect.objectContaining({
      menu_enabled: true,
      menus: [expect.objectContaining({ menu_id: 'm1', kind: 'link', pc_url: 'https://doc', sort: 0 })],
    }))
    wrapper.unmount()

    vi.mocked(feishuApps.overview).mockResolvedValue(overview({ app: { ...overview().app!, under_review: true } }))
    const review = mountPanel()
    await flushPromises()
    expect(review.text()).toContain(i18n.global.t('feishuApp.underReviewHint'))
    expect(review.get('[data-test="publish-submit"]').attributes('disabled')).toBeDefined()
    review.unmount()
  })

  it('creates slash commands without requiring a release', async () => {
    vi.mocked(feishuApps.overview).mockResolvedValue(overview())
    vi.mocked(feishuApps.createCommand).mockResolvedValue({ command_id: '10' })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-test="command-add"]').trigger('click')
    await flushPromises()
    const name = document.querySelector('input[data-test="command-name"]') as HTMLInputElement
    name.value = 'weekly'
    name.dispatchEvent(new Event('input'))
    const description = document.querySelector('input[data-test="command-description"]') as HTMLInputElement
    description.value = '生成周报'
    description.dispatchEvent(new Event('input'))
    ;(document.querySelector('[data-test="command-save"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(feishuApps.createCommand).toHaveBeenCalledWith('b1', { command: 'weekly', description: '生成周报', icon_key: 'skill_outlined' })

    // 老机器人可一键补齐 CoreMan 内置指令，完成后刷新列表。
    vi.mocked(feishuApps.addDefaultCommands).mockResolvedValue({ created: ['new', 'sessions'] })
    const reloads = vi.mocked(feishuApps.overview).mock.calls.length
    await wrapper.get('[data-test="command-defaults"]').trigger('click')
    await flushPromises()
    expect(feishuApps.addDefaultCommands).toHaveBeenCalledWith('b1')
    expect(ElMessage.success).toHaveBeenCalledWith(i18n.global.t('feishuApp.defaultCommandsAdded', { commands: '/new /sessions' }))
    expect(vi.mocked(feishuApps.overview).mock.calls.length).toBe(reloads + 1)
    wrapper.unmount()
  })

  it('explains when slash commands or the app cannot be read', async () => {
    vi.mocked(feishuApps.overview).mockResolvedValue(overview({ errors: { app: 99991672, grants: null, versions: null, slash_commands: 99991672 }, slash_commands: null }))
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain(i18n.global.t('feishuApp.appReadError', { code: 99991672 }))
    expect(wrapper.text()).toContain(i18n.global.t('feishuApp.commandsError', { code: 99991672 }))
    expect(wrapper.find('[data-test="command-add"]').exists()).toBe(false)
    wrapper.unmount()

    // 凭证无效时读不到应用：不报「缺少全部权限」，也不展示注定失败的修改表单。
    vi.mocked(feishuApps.overview).mockResolvedValue(overview({ app: null, scopes: [], missing_scopes: { tenant: [], user: [] }, pending_grants: [], errors: { app: 10003, grants: 10003, versions: null, slash_commands: 10003 }, slash_commands: null }))
    const broken = mountPanel()
    await flushPromises()
    expect(broken.get('[data-test="app-error"]').text()).toContain(i18n.global.t('feishuApp.appCredentialError', { code: 10003 }))
    for (const section of ['feishu-app-permissions', 'feishu-app-profile', 'feishu-app-bot', 'feishu-app-commands', 'feishu-app-publish']) {
      expect(broken.find(`[data-test="${section}"]`).exists(), section).toBe(false)
    }
    expect(broken.find('[data-test="feishu-app-update"]').exists()).toBe(true)
    broken.unmount()
  })
})
