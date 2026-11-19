import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { followups: vi.fn(), addFollowup: vi.fn(), updateFollowup: vi.fn(), closeFollowup: vi.fn(), rescheduleFollowup: vi.fn(), supplementFollowup: vi.fn() },
}))

import { myAssistant, type Followup, type FollowupCategory, type FollowupList } from '@/api/myAssistant'
import AssistantFollowups from '@/components/assistant/AssistantFollowups.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const item = (over: Partial<Followup>): Followup => ({
  id: 'F001', category: 'promised', status: 'open', title: '给李四报价', notes: '他周一问的', review_on: null, due_on: null,
  origin_message_id: null, created_at: '2026-11-10T00:00:00Z', due_today: false, ...over,
})
const list: FollowupList = {
  today: '2026-11-13',
  total: 3,
  groups: {
    decide: [],
    promised: [item({})],
    waiting: [item({ id: 'F002', category: 'waiting', title: '', notes: null, locked: true })],
    remind: [item({ id: 'F003', category: 'remind', title: '续约', notes: null, due_on: '2026-11-13', due_today: true })],
  },
}
interface Exposed {
  form: { category: FollowupCategory; title: string; due_on: string }
  add: () => Promise<void>
  recategorize: (row: Followup, category: FollowupCategory) => Promise<void>
}
const render = () => mount(AssistantFollowups, { global: { plugins: [ElementPlus, i18n] } })
/** 对话框挂在 body 上：取最后一个匹配的元素（之前用例留下的已卸载）。 */
const body = (selector: string) => {
  const found = [...document.body.querySelectorAll(selector)]
  expect(found.length, selector).toBeGreaterThan(0)
  return new DOMWrapper(found[found.length - 1] as HTMLElement)
}
const exposed = (wrapper: ReturnType<typeof render>) => wrapper.vm as unknown as Exposed

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.followups).mockReset().mockResolvedValue(list)
  vi.mocked(myAssistant.addFollowup).mockReset().mockResolvedValue(item({}))
  vi.mocked(myAssistant.updateFollowup).mockReset().mockResolvedValue(item({}))
  vi.mocked(myAssistant.closeFollowup).mockReset().mockResolvedValue(item({}))
  vi.mocked(myAssistant.rescheduleFollowup).mockReset().mockResolvedValue(item({}))
  vi.mocked(myAssistant.supplementFollowup).mockReset().mockResolvedValue(item({}))
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
})

describe('follow-ups', () => {
  it('shows the four categories, due marks and locked items', async () => {
    const wrapper = render(); await flushPromises()
    for (const category of ['decide', 'promised', 'waiting', 'remind']) expect(wrapper.text()).toContain(t(`assistant.followups.categories.${category}`))
    expect(wrapper.text()).toContain(t('assistant.followups.empty'))
    expect(wrapper.get('[data-test="item-F001"]').text()).toContain('给李四报价')
    expect(wrapper.get('[data-test="item-F001"]').text()).toContain('他周一问的')
    expect(wrapper.get('[data-test="item-F002"]').text()).toContain(t('assistant.page.lockedText'))
    expect(wrapper.get('[data-test="item-F003"]').text()).toContain(t('assistant.followups.dueToday'))
    wrapper.unmount()
  })

  it('asks for a close reason when its checkbox is ticked, then reloads', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="done-F001"] input').setValue(true); await flushPromises()
    expect(myAssistant.closeFollowup).not.toHaveBeenCalled()
    await body('[data-test="reason-resolved"] input').setValue(true)
    await body('textarea[data-test="action-text"]').setValue('  已经报过价 ')
    await body('[data-test="action-submit"]').trigger('click'); await flushPromises()
    expect(myAssistant.closeFollowup).toHaveBeenCalledWith('F001', 'resolved', '已经报过价')
    expect(myAssistant.followups).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('adds information to an item', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="supplement-F003"]').trigger('click'); await flushPromises()
    await body('textarea[data-test="action-text"]').setValue('找法务确认条款')
    await body('[data-test="action-submit"]').trigger('click'); await flushPromises()
    expect(myAssistant.supplementFollowup).toHaveBeenCalledWith('F003', '找法务确认条款')
    wrapper.unmount()
  })

  it('reschedules only to a date after today', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="reschedule-F001"]').trigger('click'); await flushPromises()
    const submit = body('[data-test="action-submit"]')
    expect(submit.attributes('disabled')).toBeDefined()
    // 今天是周五 11-13：明天 11-14，下周一 11-16，一周后 11-20。
    await body('[data-test="quick-nextMonday"]').trigger('click')
    await submit.trigger('click'); await flushPromises()
    expect(myAssistant.rescheduleFollowup).toHaveBeenCalledWith('F001', '2026-11-16', undefined)
    wrapper.unmount()
  })

  it('changes the category through the update endpoint and skips a no-op', async () => {
    const wrapper = render(); await flushPromises()
    await exposed(wrapper).recategorize(list.groups.promised[0], 'promised')
    expect(myAssistant.updateFollowup).not.toHaveBeenCalled()
    await exposed(wrapper).recategorize(list.groups.promised[0], 'waiting'); await flushPromises()
    expect(myAssistant.updateFollowup).toHaveBeenCalledWith('F001', { category: 'waiting' })
    wrapper.unmount()
  })

  it('adds an item with a trimmed title and refuses an empty one', async () => {
    const warning = vi.spyOn(ElMessage, 'warning').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="add"]').trigger('click'); await flushPromises()
    expect(myAssistant.addFollowup).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalledWith(t('assistant.followups.titleRequired'))
    Object.assign(exposed(wrapper).form, { category: 'decide', title: '  是否续约 ', due_on: '2026-12-01' })
    await wrapper.get('[data-test="add"]').trigger('click'); await flushPromises()
    expect(myAssistant.addFollowup).toHaveBeenCalledWith({ category: 'decide', title: '是否续约', due_on: '2026-12-01' })
    expect(exposed(wrapper).form.title).toBe('')
    wrapper.unmount()
  })

  it('shows the backend message when a change is refused', async () => {
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(myAssistant.closeFollowup).mockRejectedValueOnce(new Error('这条待跟进已经关闭'))
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="close-F001"]').trigger('click'); await flushPromises()
    await body('[data-test="reason-other"] input').setValue(true)
    await body('[data-test="action-submit"]').trigger('click'); await flushPromises()
    expect(error).toHaveBeenCalledWith('这条待跟进已经关闭')
    wrapper.unmount()
  })
})
