import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// vi.mock 的工厂会被提升到文件顶部，fixture 必须跟着 vi.hoisted 一起提上去（同 ChatLogsView.spec）。
const { ROW, ROW_OPEN_ENDED, ROW_BOUNDED } = vi.hoisted(() => {
  const base = {
    id: 'a1', scope: 'bot', relay_server_id: null, relay_name: null, bot_id: 'b1', bot_key: 'sales_bot', bot_name: '销售助手',
    content: '系统维护中，请稍后再试', is_active: true, start_at: null, end_at: null, time_status: 'active',
    created_by: 'u1', created_at: '2026-09-12T00:00:00Z', updated_at: '2026-09-12T00:00:00Z',
  }
  return {
    ROW: base,
    // 半开区间：只配开始、永不过期，是后端的一等状态（start_at 过去 + end_at 为空 = 生效中）。
    ROW_OPEN_ENDED: { ...base, id: 'a2', start_at: '2026-09-01T00:00:00Z', end_at: null },
    ROW_BOUNDED: { ...base, id: 'a3', start_at: '2026-09-01T00:00:00Z', end_at: '2026-10-01T00:00:00Z' },
  }
})

vi.mock('@/api/admin', () => ({
  announcements: {
    list: vi.fn().mockResolvedValue([ROW]),
    create: vi.fn().mockResolvedValue(ROW),
    update: vi.fn().mockResolvedValue(ROW),
    toggle: vi.fn().mockResolvedValue({ ...ROW, is_active: false }),
    remove: vi.fn().mockResolvedValue(null),
  },
  bots: { list: vi.fn().mockResolvedValue({ items: [{ id: 'b1', bot_key: 'sales_bot', name: '销售助手' }], total: 1, page: 1, per_page: 200 }) },
  relays: { list: vi.fn().mockResolvedValue({ items: [{ id: 'r1', name: 'claude01' }], total: 1, page: 1, per_page: 200 }) },
}))

import { announcements } from '@/api/admin'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import AnnouncementsView from '@/views/AnnouncementsView.vue'

function login(role: string) {
  useAuthStore().user = { id: 'me', login_name: 'c', display_name: 'C', role, locale: 'zh' } as never
}

/** 列表里只放这一行，然后点它的「编辑」，停在打开的弹窗上。 */
async function openEditorFor(row: { id: string }) {
  login('platform_admin')
  vi.mocked(announcements.update).mockClear()
  vi.mocked(announcements.list).mockResolvedValueOnce([row] as never)
  const wrapper = mount(AnnouncementsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
  await flushPromises()
  await wrapper.get(`[data-test="edit-${row.id}"]`).trigger('click')
  await flushPromises()
  return wrapper
}

/** 弹窗 teleport 到 body；断言失败的用例来不及 unmount 会留下旧弹窗，所以点最新挂上去的那一个。 */
async function clickSave() {
  const buttons = document.querySelectorAll<HTMLButtonElement>('[data-test="save-announcement"]')
  buttons[buttons.length - 1]!.click()
  await flushPromises()
}

/** 最后一次 update 的请求体。时间只比较瞬间，不比较字符串写法（Date#toISOString 会归一到 Z）。 */
function lastUpdateBody() {
  const calls = vi.mocked(announcements.update).mock.calls
  expect(calls.length).toBeGreaterThan(0)
  return calls[calls.length - 1][1]
}

function instantOf(value: string | null | undefined) {
  return value == null ? null : new Date(value).getTime()
}

describe('AnnouncementsView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('lists announcements with scope, target and status', async () => {
    login('ai_committee')
    const wrapper = mount(AnnouncementsView, { global: { plugins: [ElementPlus, i18n] } })
    await flushPromises()
    const row = wrapper.get('[data-test="row-a1"]')
    expect(row.text()).toContain(i18n.global.t('announcements.scopes.bot'))
    expect(row.text()).toContain('销售助手')
    expect(row.text()).toContain('系统维护中')
    expect(row.text()).toContain(i18n.global.t('announcements.statuses.active'))
    wrapper.unmount()
  })

  it('creates a bot-scoped announcement', async () => {
    login('platform_admin')
    const wrapper = mount(AnnouncementsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="create-announcement"]').trigger('click')
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { scope: string; bot_id: string | null; relay_server_id: string | null; content: string; is_active: boolean } }
    vm.form.scope = 'bot'
    vm.form.bot_id = 'b1'
    vm.form.content = '维护中'
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-test="save-announcement"]')!.click()
    await flushPromises()
    expect(announcements.create).toHaveBeenCalledWith({ scope: 'bot', bot_id: 'b1', relay_server_id: null, content: '维护中', is_active: true, start_at: null, end_at: null })
    wrapper.unmount()
  })

  // 切换范围必须把另一条目标 id 清掉，否则后端会拿「范围与目标不匹配」422 打回来。
  it('clears the other target id when the scope changes', async () => {
    login('platform_admin')
    const wrapper = mount(AnnouncementsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="create-announcement"]').trigger('click')
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { scope: string; bot_id: string | null; relay_server_id: string | null; content: string }
      onScopeChange: (scope: string) => void
    }
    vm.form.scope = 'bot'
    vm.form.bot_id = 'b1'
    await flushPromises()
    // 目标下拉只显示当前范围对应的那一个。
    expect(document.querySelectorAll('[data-test="form-target"]').length).toBe(1)
    vm.onScopeChange('relay')
    await flushPromises()
    expect(vm.form.bot_id).toBeNull()
    vm.form.relay_server_id = 'r1'
    vm.form.content = '维护中'
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-test="save-announcement"]')!.click()
    await flushPromises()
    expect(announcements.create).toHaveBeenLastCalledWith({ scope: 'relay', bot_id: null, relay_server_id: 'r1', content: '维护中', is_active: true, start_at: null, end_at: null })
    wrapper.unmount()
  })

  it('toggles and deletes after confirm', async () => {
    login('ai_committee')
    // 同一文件里的 mock 会跨用例累计调用次数，这里只想数本用例内的刷新。
    vi.mocked(announcements.list).mockClear()
    const wrapper = mount(AnnouncementsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="toggle-a1"]').trigger('click')
    await flushPromises()
    expect(announcements.toggle).toHaveBeenCalledWith('a1')
    // 启停与删除之后都要重新拉列表，否则页面还显示旧状态。
    expect(announcements.list).toHaveBeenCalledTimes(2)
    const { ElMessageBox } = await import('element-plus')
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    await wrapper.get('[data-test="delete-a1"]').trigger('click')
    await flushPromises()
    expect(announcements.remove).toHaveBeenCalledWith('a1')
    expect(announcements.list).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('does not delete when the confirm dialog is dismissed', async () => {
    login('ai_committee')
    vi.mocked(announcements.remove).mockClear()
    const { ElMessageBox } = await import('element-plus')
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel' as never)
    const wrapper = mount(AnnouncementsView, { global: { plugins: [ElementPlus, i18n] }, attachTo: document.body })
    await flushPromises()
    await wrapper.get('[data-test="delete-a1"]').trigger('click')
    await flushPromises()
    expect(announcements.remove).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  // 只配了开始、永不过期的公告，编辑时不能被压成「开始=结束」的零长度窗口（那等于把公告悄悄关掉）。
  it('keeps an open-ended window open when only the content is edited', async () => {
    const wrapper = await openEditorFor(ROW_OPEN_ENDED)
    // 时间窗是两个各自可清空的时间点，不是一个 datetimerange。
    const windowItem = document.querySelectorAll('[data-test="form-window"]')
    expect(windowItem.length).toBe(1)
    expect(windowItem[0]!.querySelectorAll('[data-test="form-start"]').length).toBe(1)
    expect(windowItem[0]!.querySelectorAll('[data-test="form-end"]').length).toBe(1)
    const vm = wrapper.vm as unknown as { form: { content: string } }
    vm.form.content = '改个错别字'
    await flushPromises()
    await clickSave()
    const body = lastUpdateBody()
    expect(instantOf(body.start_at)).toBe(new Date('2026-09-01T00:00:00Z').getTime())
    expect(body.end_at).toBeNull()
    expect(body.content).toBe('改个错别字')
    wrapper.unmount()
  })

  it('keeps both bounds null when an unbounded announcement is saved untouched', async () => {
    const wrapper = await openEditorFor(ROW)
    await clickSave()
    const body = lastUpdateBody()
    expect(body.start_at).toBeNull()
    expect(body.end_at).toBeNull()
    wrapper.unmount()
  })

  it('preserves both bounds of a fully bounded window', async () => {
    const wrapper = await openEditorFor(ROW_BOUNDED)
    await clickSave()
    const body = lastUpdateBody()
    expect(instantOf(body.start_at)).toBe(new Date('2026-09-01T00:00:00Z').getTime())
    expect(instantOf(body.end_at)).toBe(new Date('2026-10-01T00:00:00Z').getTime())
    wrapper.unmount()
  })
})
