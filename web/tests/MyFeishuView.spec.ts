import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { i18n } from '@/i18n'
import MyFeishuView from '@/views/MyFeishuView.vue'
import { feishuAuthorizations } from '@/api/feishuAuthorizations'
vi.mock('@/api/feishuAuthorizations', () => ({ feishuAuthorizations: { list: vi.fn(), revoke: vi.fn() } }))
const row = { bot_id: 'bot-1', bot_name: 'Personal Assistant', status: 'connected' as const, scopes: ['im:message:readonly'], expires_at: null }
const render = () => mount(MyFeishuView, { global: { plugins: [ElementPlus, i18n] } })
beforeEach(() => { vi.restoreAllMocks(); vi.mocked(feishuAuthorizations.list).mockReset().mockResolvedValue({ items: [row] }); vi.mocked(feishuAuthorizations.revoke).mockReset().mockResolvedValue({ ok: true, remote_revoked: true }) })
describe('own Feishu authorizations', () => {
  it('shows private-chat instructions and all statuses without a browser connect action', async () => {
    vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: ['connected', 'pending', 'expired', 'revoked'].map((status) => ({ ...row, bot_id: status, status: status as typeof row.status })) })
    const w = render(); await flushPromises()
    expect(w.text()).toContain('连接飞书')
    expect(w.text()).toContain('无需命令前缀')
    expect(w.text()).not.toContain('/飞书个人')
    // 个人工具叠加在普通私聊上：不再有“普通助手 / 飞书资料”模式切换，也没有只存内存的思考过程。
    for (const stale of ['普通助手', '“飞书资料”', '资料模式', '24 小时']) expect(w.text()).not.toContain(stale)
    expect(w.get('.feishu-connect-guide').text()).toContain(i18n.global.t('myFeishu.toolsHint'))
    expect(w.text()).toContain('卡片')
    expect(w.text()).toContain(i18n.global.t('myFeishu.privateOnly'))
    for (const status of ['connected', 'pending', 'expired', 'revoked']) expect(w.text()).toContain(i18n.global.t(`myFeishu.status.${status}`))
    expect(w.find('[data-test="connect"]').exists()).toBe(false)
    expect(w.find('[data-test="revoke-revoked"]').exists()).toBe(false)
    expect(w.text()).toContain(i18n.global.t('myFeishu.pendingExpiresAt'))
    expect(w.text()).toContain(i18n.global.t('myFeishu.expiresAt'))
    w.unmount()
  })
  it('cancelling confirmation does not revoke', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    const w = render(); await flushPromises(); await w.get('[data-test="revoke-bot-1"]').trigger('click'); await flushPromises()
    expect(feishuAuthorizations.revoke).not.toHaveBeenCalled()
    expect(ElMessageBox.confirm).toHaveBeenCalledWith(i18n.global.t('myFeishu.confirmRevoke', { name: row.bot_name }), i18n.global.t('myFeishu.revoke'), { type: 'warning' })
    w.unmount()
  })
  it('revokes the selected bot and refreshes its visible state', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const w = render(); await flushPromises()
    vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: [{ ...row, status: 'revoked' }] })
    await w.get('[data-test="revoke-bot-1"]').trigger('click'); await flushPromises()
    expect(feishuAuthorizations.revoke).toHaveBeenCalledWith('bot-1')
    expect(w.text()).toContain(i18n.global.t('myFeishu.status.revoked'))
    expect(w.find('[data-test="revoke-bot-1"]').exists()).toBe(false); w.unmount()
  })
  it('offers retry after list failure without displaying raw upstream errors', async () => {
    vi.mocked(feishuAuthorizations.list).mockRejectedValueOnce(new Error('secret-token'))
    const w = render(); await flushPromises()
    expect(w.text()).toContain(i18n.global.t('myFeishu.loadError')); expect(w.text()).not.toContain('secret-token')
    await w.findAll('button').find(b => b.text() === i18n.global.t('workspace.retry'))!.trigger('click'); await flushPromises()
    expect(w.text()).toContain(row.bot_name); w.unmount()
  })
})

it('keeps access visible when revocation fails, and hides raw error details', async () => {
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  vi.mocked(feishuAuthorizations.revoke).mockRejectedValueOnce(new Error('secret-token'))
  const w = render(); await flushPromises()
  await w.get('[data-test="revoke-bot-1"]').trigger('click'); await flushPromises()
  expect(w.text()).toContain(i18n.global.t('myFeishu.status.connected'))
  expect(w.get('[data-test="revoke-bot-1"]').attributes('disabled')).toBeUndefined()
  expect(w.text()).not.toContain('secret-token')
  w.unmount()
})

it('automatically discovers authorization completed outside the page and stops after leaving', async () => {
  vi.useFakeTimers()
  vi.mocked(feishuAuthorizations.list).mockResolvedValueOnce({ items: [] })
  const w = render()
  try {
    await flushPromises()
    expect(w.text()).toContain(i18n.global.t('myFeishu.empty'))
    await vi.advanceTimersByTimeAsync(15000)
    await flushPromises()
    expect(w.text()).toContain(row.bot_name)
    w.unmount()
    const calls = vi.mocked(feishuAuthorizations.list).mock.calls.length
    window.dispatchEvent(new Event('focus'))
    await vi.advanceTimersByTimeAsync(30000)
    expect(feishuAuthorizations.list).toHaveBeenCalledTimes(calls)
  } finally { vi.useRealTimers() }
})
it('refreshes when returning from Feishu without replacing the current card with a loading screen', async () => {
  const w = render(); await flushPromises()
  let resolve!: (value: { items: typeof row[] }) => void
  vi.mocked(feishuAuthorizations.list).mockReturnValueOnce(new Promise(r => { resolve = r }))
  window.dispatchEvent(new Event('focus'))
  await flushPromises()
  expect(feishuAuthorizations.list).toHaveBeenCalledTimes(2)
  expect(w.text()).toContain(row.bot_name)
  resolve({ items: [] }); await flushPromises()
  expect(w.text()).toContain(i18n.global.t('myFeishu.empty'))
  w.unmount()
})
it('does not let a stale automatic refresh restore access after revocation', async () => {
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const w = render(); await flushPromises()
  let resolve!: (value: { items: typeof row[] }) => void
  vi.mocked(feishuAuthorizations.list).mockReturnValueOnce(new Promise(r => { resolve = r }))
  window.dispatchEvent(new Event('focus')); await flushPromises()
  vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: [{ ...row, status: 'revoked' }] })
  await w.get('[data-test="revoke-bot-1"]').trigger('click'); await flushPromises()
  resolve({ items: [row] }); await flushPromises()
  expect(w.text()).toContain(i18n.global.t('myFeishu.status.revoked'))
  expect(w.find('[data-test="revoke-bot-1"]').exists()).toBe(false)
  w.unmount()
})

it('shows five scopes per bot and expands each bot independently', async () => {
  const scopes = Array.from({ length: 9 }, (_, i) => `scope:${i}`)
  vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: [{ ...row, scopes }, { ...row, bot_id: 'bot-2', scopes }] })
  const w = render(); await flushPromises()
  expect(w.findAll('.feishu-scope')).toHaveLength(10)
  await w.get('[data-test="toggle-scopes-bot-1"]').trigger('click')
  expect(w.findAll('.feishu-scope')).toHaveLength(14)
  expect(w.get('[data-test="toggle-scopes-bot-2"]').attributes('aria-expanded')).toBe('false')
  await w.get('[data-test="toggle-scopes-bot-1"]').trigger('click')
  expect(w.findAll('.feishu-scope')).toHaveLength(10)
  w.unmount()
})


it('distinguishes the selected tier from Feishu grants and missing permissions', async () => {
  vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: [{ ...row, authorization_level: 'all_except_send', requested_scopes: ['doc:write'], missing_scopes: ['doc:write'], scopes: ['im:message:send'] }] })
  const w = render(); await flushPromises()
  expect(w.text()).toContain(i18n.global.t('myFeishu.levels.all_except_send'))
  expect(w.text()).toContain(i18n.global.t('myFeishu.scopeHint'))
  expect(w.get('.feishu-requested-scope').text()).toBe('doc:write')
  expect(w.get('.feishu-scope').text()).toBe('im:message:send')
  expect(w.get('.feishu-missing-scope').text()).toBe('doc:write')
  w.unmount()
})
it('does not claim remote revocation when only local access has stopped', async () => {
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
  const success = vi.spyOn(ElMessage, 'success')
  const warning = vi.spyOn(ElMessage, 'warning')
  vi.mocked(feishuAuthorizations.revoke).mockResolvedValue({ ok: true, remote_revoked: false })
  const w = render(); await flushPromises()
  vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: [{ ...row, status: 'revoked' }] })
  await w.get('[data-test="revoke-bot-1"]').trigger('click'); await flushPromises()
  expect(success).not.toHaveBeenCalled()
  expect(warning).toHaveBeenCalledWith(i18n.global.t('myFeishu.localRevoked'))
  w.unmount()
})


it('does not present the default tier as the users choice while selecting', async () => {
  vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: [{ ...row, status: 'selecting', authorization_level: 'messages_readonly', scopes: [] }] })
  const w = render(); await flushPromises()
  const card = w.get('.feishu-grant')
  expect(card.text()).toContain(i18n.global.t('myFeishu.unselected'))
  expect(card.text()).not.toContain(i18n.global.t('myFeishu.levels.messages_readonly'))
  w.unmount()
})

it('keeps historical credential scopes inside collapsed advanced details', async () => {
  vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: [{ ...row, authorization_level: 'messages_readonly', requested_scopes: ['im:message:readonly'], scopes: ['im:message:readonly', 'im:message.send_as_user'] }] })
  const w = render(); await flushPromises()
  const card = w.get('.feishu-grant')
  expect(card.get('.feishu-advanced').attributes('open')).toBeUndefined()
  expect(card.get('.feishu-advanced').text()).toContain('im:message.send_as_user')
  expect(card.get('.feishu-permission-overview').text()).toContain(i18n.global.t('myFeishu.capabilityHints.messages_readonly'))
  expect(card.get('.feishu-permission-overview').text()).not.toContain('im:message.send_as_user')
  w.unmount()
})

it.each([true, false])('retains audit disclosure and expiry guidance with refresh=%s', async (refresh_available) => {
  vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: [{ ...row, access_token_expired: true, refresh_available }] })
  const w = render(); await flushPromises()
  expect(w.get('.feishu-connect-guide').text()).toContain(i18n.global.t('myFeishu.retentionNotice'))
  expect(w.get('.feishu-connect-guide').text()).toContain(i18n.global.t('myFeishu.toolsHint'))
  expect(w.get('.feishu-grant').text()).toContain(i18n.global.t(refresh_available ? 'myFeishu.tokenRefreshAvailable' : 'myFeishu.tokenExpired'))
  expect(w.get('.feishu-grant').text()).toContain(i18n.global.t('myFeishu.status.connected'))
  w.unmount()
})
