import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { i18n } from '@/i18n'
import MyFeishuView from '@/views/MyFeishuView.vue'
import { feishuAuthorizations } from '@/api/feishuAuthorizations'
vi.mock('@/api/feishuAuthorizations', () => ({ feishuAuthorizations: { list: vi.fn(), revoke: vi.fn() } }))
const row = { bot_id: 'bot-1', bot_name: 'Personal Assistant', status: 'connected' as const, scopes: ['im:message:readonly'], expires_at: null }
const render = () => mount(MyFeishuView, { global: { plugins: [ElementPlus, i18n] } })
beforeEach(() => { vi.restoreAllMocks(); vi.mocked(feishuAuthorizations.list).mockReset().mockResolvedValue({ items: [row] }); vi.mocked(feishuAuthorizations.revoke).mockReset().mockResolvedValue({ ok: true }) })
describe('own Feishu authorizations', () => {
  it('shows private-chat instructions and all statuses without a browser connect action', async () => {
    vi.mocked(feishuAuthorizations.list).mockResolvedValue({ items: ['connected', 'pending', 'expired', 'revoked'].map((status) => ({ ...row, bot_id: status, status: status as typeof row.status })) })
    const w = render(); await flushPromises()
    expect(w.text()).toContain('连接我的飞书')
    expect(w.text()).toContain('无需命令前缀')
    expect(w.text()).not.toContain('/飞书个人')
    expect(w.text()).toContain('Claude')
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
