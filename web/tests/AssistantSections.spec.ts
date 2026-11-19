import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Component } from 'vue'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { board: vi.fn(), closeFollowup: vi.fn(), rescheduleFollowup: vi.fn(), supplementFollowup: vi.fn(), declineGap: vi.fn(), grantGap: vi.fn(), requestGap: vi.fn(), withdrawGap: vi.fn(), jobs: vi.fn(), cancelJob: vi.fn(), recentApprovals: vi.fn() },
}))

import { BOARD_GROUPS, myAssistant, type AssistantJob, type Board, type BoardItem, type SettledApproval } from '@/api/myAssistant'
import AssistantBoard from '@/components/assistant/AssistantBoard.vue'
import AssistantJobs from '@/components/assistant/AssistantJobs.vue'
import AssistantRecent from '@/components/assistant/AssistantRecent.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const job = (over: Partial<AssistantJob>): AssistantJob => ({
  id: 'j1', kind: 'subtask', status: 'queued', title: '退货原因', rounds: 1, created_at: '2026-11-13T01:00:00Z',
  finished_at: null, has_result: false, brief: '查一下上季度的退货原因', result: null, blocked_reason: null, ...over,
})
const item = (over: Partial<BoardItem>): BoardItem => ({
  id: 'F001', category: 'decide', status: 'open', title: '选供应商', review_on: null, due_on: null,
  origin_message_id: null, created_at: '2026-11-13T00:00:00Z', needs_owner: false, has_notes: true,
  handler: 'assistant', handler_name: null, job_id: null, progress_at: '2026-11-13T00:00:00Z',
  stage: 'owner', reason: 'decide', idle_days: 0, suggestion: '选 A', need: null, stuck: null, nudges: 0, job: null, gap: null, approvals: [], ...over,
})
const boardData: Board = {
  today: '2026-11-18',
  counts: { owner: 1, working: 0, others: 0, scheduled: 0, stalled: 1 },
  bot_id: 'b1',
  gaps: [],
  approvals: [
    { id: 'a1', tool: 'feishu_im_send_message', summary: '私聊 → 李四', created_at: '2026-11-13T01:00:00Z', link: 'https://tower.example.com' },
    { id: 'a2', tool: 'feishu_doc_update', summary: null, created_at: '2026-11-13T02:00:00Z', link: null },
  ],
  approvals_total: 2,
  groups: {
    owner: [item({})],
    stalled: [item({ id: 'F002', category: 'waiting', stage: 'stalled', reason: 'no_reply', nudges: 2, idle_days: 6, handler: 'person', handler_name: '赵六', suggestion: null })],
    working: [], others: [], scheduled: [],
  },
}
const settled = (over: Partial<SettledApproval>): SettledApproval => ({
  id: 's1', tool: 'feishu_im_send_message', channel: 'p2p', status: 'executed', summary: '私聊 → 李四', reason: null, error: null,
  created_at: '2026-11-12T01:00:00Z', decided_at: '2026-11-12T02:00:00Z', executed_at: '2026-11-12T02:00:05Z', locked: false, ...over,
})
const render = (component: Component) => mount(component, {
  global: { plugins: [ElementPlus, i18n], stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
})
/** 对话框与气泡卡片原地渲染（不挪到 body），便于在组件里找。 */
const renderInline = (component: Component) => mount(component, {
  global: { plugins: [ElementPlus, i18n], stubs: { teleport: true } },
})

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.board).mockReset().mockResolvedValue(boardData)
  vi.mocked(myAssistant.jobs).mockReset().mockResolvedValue({ active: [job({})], recent: [job({ id: 'j2', status: 'done', title: '已完成的调查', result: '结论在此', finished_at: '2026-11-12T03:00:00Z', has_result: true })] })
  vi.mocked(myAssistant.cancelJob).mockReset().mockResolvedValue({ id: 'j1', status: 'cancelled' })
  vi.mocked(myAssistant.recentApprovals).mockReset().mockResolvedValue({ items: [settled({}), settled({ id: 's2', status: 'rejected', reason: '语气太硬' }), settled({ id: 's3', status: 'withdrawn', summary: null, locked: true })] })
})

describe('board', () => {
  it('lists gaps with how to enable them and declines after confirmation', async () => {
    const gap = { id: 'G003', kind: 'feishu_scope' as const, key: 'im:message', need: '读群消息', status: 'open' as const, fix: 'authorize' as const, hint: '', followups: ['F001'], request: null, need_write: false, stage: 'owner' as const }
    const admin = { ...gap, id: 'G004', kind: 'business_system' as const, key: 'orders', fix: 'self_grant' as const, followups: [] }
    vi.mocked(myAssistant.board).mockResolvedValue({
      ...boardData,
      groups: { ...boardData.groups, owner: [item({ id: 'F001', reason: 'gap', gap: 'G003' })] },
      gaps: [gap, admin],
    })
    vi.mocked(myAssistant.declineGap).mockResolvedValue({ id: 'G003', status: 'declined' })
    vi.mocked(myAssistant.grantGap).mockResolvedValue({ id: 'G004', status: 'resolved' })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const wrapper = render(AssistantBoard); await flushPromises()
    const row = wrapper.get('[data-test="gap-G003"]').text()
    expect(row).toContain('im:message')
    expect(row).toContain('F001')
    expect(row).toContain(t('assistant.board.gap.fixes.authorize', { name: 'G003' }))
    expect(wrapper.find('[data-test="gap-G003"] [data-test="gap-grant"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="gap-G004"]').text()).toContain(t('assistant.board.gap.fixes.self_grant'))
    await wrapper.get('[data-test="gap-G003"] [data-test="gap-decline"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalled()
    expect(myAssistant.declineGap).toHaveBeenCalledWith('G003')
    expect(myAssistant.board).toHaveBeenCalledTimes(2)
    await wrapper.get('[data-test="gap-G004"] [data-test="gap-grant"]').trigger('click'); await flushPromises()
    expect(myAssistant.grantGap).toHaveBeenCalledWith('G004')
    expect(myAssistant.board).toHaveBeenCalledTimes(3)
    // 所有者自己能开的缺口列在「等你」里，卡住的事项标着原因。
    expect(wrapper.get('[data-test="board-F001"]').text()).toContain(t('assistant.board.reasons.gap'))
    expect(wrapper.get('[data-test="group-owner"]').text()).toContain(t('assistant.board.gapsHeading.owner', { n: 2 }))
    wrapper.unmount()
  })

  it('shows the error and reloads when the gap was resolved meanwhile', async () => {
    const gap = { id: 'G003', kind: 'other' as const, key: 'repo', need: '读仓库', status: 'open' as const, fix: 'none' as const, hint: '', followups: [], request: null, need_write: false, stage: 'stalled' as const }
    vi.mocked(myAssistant.board).mockResolvedValue({ ...boardData, gaps: [gap] })
    vi.mocked(myAssistant.declineGap).mockRejectedValue(new Error('这个缺口已经可以用了'))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const error = vi.spyOn(ElMessage, 'error').mockImplementation((() => undefined) as never)
    const wrapper = render(AssistantBoard); await flushPromises()
    await wrapper.get('[data-test="gap-decline"]').trigger('click'); await flushPromises()
    expect(error).toHaveBeenCalled()
    expect(wrapper.find('[data-test="gap-G003"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('submits a request with the reason the owner typed, then shows it as requested with withdraw', async () => {
    const gap = { id: 'G005', kind: 'business_system' as const, key: 'orders', need: '查订单 内部内容', status: 'open' as const, fix: 'request' as const, hint: '', followups: [], request: null, need_write: false, stage: 'owner' as const }
    vi.mocked(myAssistant.board).mockResolvedValue({ ...boardData, gaps: [gap] })
    vi.mocked(myAssistant.requestGap).mockResolvedValue({ id: 'G005', status: 'requested', request_id: 'r1' })
    const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: '查客户订单状态', action: 'confirm' } as never)
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.find('[data-test="gap-grant"]').exists()).toBe(false)
    await wrapper.get('[data-test="gap-request"]').trigger('click'); await flushPromises()
    // 不把助理记下的需要预填进给管理员看的理由。
    expect(JSON.stringify(prompt.mock.calls[0])).not.toContain('内部内容')
    expect(myAssistant.requestGap).toHaveBeenCalledWith('G005', '查客户订单状态')
    wrapper.unmount()

    const requested = { ...gap, status: 'requested' as const, request: { status: 'pending' as const, reason: '查客户订单状态', decision_note: null, created_at: '2026-11-18T00:00:00Z' }, need_write: false }
    vi.mocked(myAssistant.board).mockResolvedValue({ ...boardData, gaps: [requested] })
    vi.mocked(myAssistant.withdrawGap).mockResolvedValue({ id: 'G005', status: 'open' })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel' as never).mockResolvedValue('confirm' as never)
    const again = render(AssistantBoard); await flushPromises()
    expect(again.get('[data-test="gap-G005"]').text()).toContain(t('assistant.board.gap.requested'))
    expect(again.find('[data-test="gap-request"]').exists()).toBe(false)
    // 先取消一次：不撤回；确认后才撤回。
    await again.get('[data-test="gap-withdraw"]').trigger('click'); await flushPromises()
    expect(myAssistant.withdrawGap).not.toHaveBeenCalled()
    await again.get('[data-test="gap-withdraw"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(myAssistant.withdrawGap).toHaveBeenCalledWith('G005')
    again.unmount()
  })

  it('shows why the last request was rejected', async () => {
    const gap = { id: 'G006', kind: 'business_system' as const, key: 'orders', need: '查订单', status: 'open' as const, fix: 'request' as const, hint: '', followups: [], request: { status: 'rejected' as const, reason: '查订单', decision_note: '用报表即可', created_at: '2026-11-18T00:00:00Z' }, need_write: false, stage: 'owner' as const }
    vi.mocked(myAssistant.board).mockResolvedValue({ ...boardData, gaps: [gap] })
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.get('[data-test="gap-rejected"]').text()).toContain(t('assistant.board.gap.rejected', { value: '用报表即可' }))
    expect(wrapper.find('[data-test="gap-request"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows the needed level and reports a grant that did not resolve the gap', async () => {
    const gap = { id: 'G007', kind: 'business_system' as const, key: 'orders', need: '改订单', status: 'open' as const, fix: 'self_grant' as const, hint: '', followups: [], request: null, need_write: true, stage: 'owner' as const }
    vi.mocked(myAssistant.board).mockResolvedValue({ ...boardData, gaps: [gap] })
    vi.mocked(myAssistant.grantGap).mockResolvedValue({ id: 'G007', status: 'open' })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const success = vi.spyOn(ElMessage, 'success').mockImplementation((() => undefined) as never)
    const warning = vi.spyOn(ElMessage, 'warning').mockImplementation((() => undefined) as never)
    const wrapper = render(AssistantBoard); await flushPromises()
    const write = t('assistant.board.gap.levels.write')
    expect(wrapper.get('[data-test="gap-level"]').text()).toBe(t('assistant.board.gap.needLevel', { value: write }))
    await wrapper.get('[data-test="gap-grant"]').trigger('click'); await flushPromises()
    expect(String(confirm.mock.calls[0][0])).toContain(write)
    expect(myAssistant.grantGap).toHaveBeenCalledWith('G007')
    expect(success).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalledWith(t('assistant.board.gap.grantIncomplete'))
    wrapper.unmount()
  })

  it('says the last request was rejected without an empty reason', async () => {
    const gap = { id: 'G008', kind: 'business_system' as const, key: 'orders', need: '查订单', status: 'open' as const, fix: 'request' as const, hint: '', followups: [], request: { status: 'rejected' as const, reason: '查订单', decision_note: null, created_at: '2026-11-18T00:00:00Z' }, need_write: false, stage: 'owner' as const }
    vi.mocked(myAssistant.board).mockResolvedValue({ ...boardData, gaps: [gap] })
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.get('[data-test="gap-rejected"]').text()).toBe(t('assistant.board.gap.rejectedPlain'))
    wrapper.unmount()
  })

  it('hides the need text of a locked gap', async () => {
    const gap = { id: 'G005', kind: 'other' as const, key: 'repo', need: null, status: 'open' as const, fix: 'none' as const, hint: '', followups: [], request: null, need_write: false, locked: true, stage: 'stalled' as const }
    vi.mocked(myAssistant.board).mockResolvedValue({ ...boardData, gaps: [gap] })
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.get('[data-test="gap-G005"]').text()).toContain(t('assistant.page.lockedText'))
    wrapper.unmount()
  })

  it('lists approvals with a safe link and decisions under 等你, stalled items with who and how long', async () => {
    const wrapper = render(AssistantBoard); await flushPromises()
    const first = wrapper.get('[data-test="approval-a1"]')
    expect(first.text()).toContain('私聊 → 李四')
    const link = first.get('a')
    expect(link.attributes('href')).toBe('https://tower.example.com')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    const second = wrapper.get('[data-test="approval-a2"]')
    expect(second.find('a').exists()).toBe(false)
    expect(second.text()).toContain(t('assistant.attention.noLink'))
    expect(second.text()).toContain(t('assistant.page.lockedText'))
    expect(wrapper.get('[data-test="board-F001"]').text()).toContain('建议：选 A')
    const stalled = wrapper.get('[data-test="board-F002"]').text()
    expect(stalled).toContain('赵六 在办')
    expect(stalled).toContain('6 个工作日没有进展')
    expect(stalled).toContain(t('assistant.board.reasons.no_reply'))
    expect(stalled).toContain(t('assistant.board.nudged', { n: 2 }))
    expect(wrapper.text()).toContain('没有正在执行的事。')
    wrapper.unmount()
  })

  it('shows why a linked sub-task is blocked', async () => {
    vi.mocked(myAssistant.board).mockResolvedValue({
      ...boardData,
      groups: { ...boardData.groups, owner: [item({ job: job({ id: 'j9', status: 'blocked', title: '合同条款', blocked_reason: '需要你确认范围' }) })] },
    })
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.get('[data-test="board-F001"]').text()).toContain('需要你确认范围')
    wrapper.unmount()
  })

  it('shows an approval under the follow-up it belongs to', async () => {
    const linked = { id: 'a9', tool: 'feishu_im_send_message', summary: '私聊 → 李四', created_at: '2026-11-13T01:00:00Z', link: 'https://tower.example.com' }
    vi.mocked(myAssistant.board).mockResolvedValue({
      ...boardData,
      groups: { ...boardData.groups, owner: [item({ approvals: [linked] })] },
    })
    const wrapper = render(AssistantBoard); await flushPromises()
    const row = wrapper.get('[data-test="board-F001"]')
    expect(row.text()).toContain(t('assistant.board.linkedApproval', { value: '私聊 → 李四' }))
    expect(row.get('a').attributes('href')).toBe('https://tower.example.com')
    wrapper.unmount()
  })

  it('says who is on it the same way the card does', async () => {
    vi.mocked(myAssistant.board).mockResolvedValue({
      ...boardData,
      groups: {
        ...boardData.groups,
        working: [
          item({ id: 'F003', stage: 'working', handler: 'job', job: job({ kind: 'bot_help' }) }),
          item({ id: 'F004', stage: 'working', handler: 'job', job: job({ id: 'j5' }) }),
          item({ id: 'F005', stage: 'working', handler: 'job', job: null }),
        ],
        others: [item({ id: 'F006', stage: 'others', handler: 'person', handler_name: null, locked: true })],
      },
    })
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.get('[data-test="board-F003"]').text()).toContain(t('assistant.board.handler.botHelp'))
    expect(wrapper.get('[data-test="board-F004"]').text()).toContain(t('assistant.board.handler.job'))
    expect(wrapper.get('[data-test="board-F005"]').text()).toContain(t('assistant.board.handler.assistant'))
    expect(wrapper.get('[data-test="board-F006"]').text()).toContain(t('assistant.board.handler.colleague'))
    wrapper.unmount()
  })

  it('shows a batched request once with its item count and how many more are not listed', async () => {
    vi.mocked(myAssistant.board).mockResolvedValue({
      ...boardData,
      approvals: [{ ...boardData.approvals[0], items: 3 }],
      approvals_total: 12,
    })
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.get('[data-test="approval-a1"]').text()).toContain(t('assistant.board.batchItems', { n: 3 }))
    expect(wrapper.get('[data-test="approvals-more"]').text()).toBe(t('assistant.board.moreApprovals', { n: 11 }))
    wrapper.unmount()
  })

  it('marks a dispatch that is waiting for approval', async () => {
    vi.mocked(myAssistant.board).mockResolvedValue({
      ...boardData,
      approvals: [{ id: 'a3', tool: 'assistant_ask_bot', summary: '调度员工 → demo-bot', created_at: '2026-11-13T03:00:00Z', link: null }],
    })
    const wrapper = render(AssistantBoard); await flushPromises()
    const row = wrapper.get('[data-test="approval-a3"]')
    expect(row.text()).toContain(t('assistant.attention.dispatch'))
    expect(row.text()).toContain('调度员工 → demo-bot')
    wrapper.unmount()
  })

  it('labels writes staged while the owner was away with the system and operation', async () => {
    vi.mocked(myAssistant.board).mockResolvedValue({
      ...boardData,
      approvals: [
        { id: 'a4', tool: 'systems_call', summary: '业务系统 · 库存 · 创建草稿', created_at: '2026-11-13T03:00:00Z', link: null },
        { id: 'a5', tool: 'cloud_call', summary: '云服务 · slb · AddAccessControlListEntry', created_at: '2026-11-13T03:00:00Z', link: null },
      ],
    })
    const wrapper = render(AssistantBoard); await flushPromises()
    const system = wrapper.get('[data-test="approval-a4"]')
    expect(system.text()).toContain(t('assistant.attention.systemsCall'))
    expect(system.text()).toContain('业务系统 · 库存 · 创建草稿')
    const cloud = wrapper.get('[data-test="approval-a5"]')
    expect(cloud.text()).toContain(t('assistant.attention.cloudCall'))
    expect(cloud.text()).toContain('AddAccessControlListEntry')
    expect(cloud.text()).not.toContain(t('assistant.attention.dispatch'))
    wrapper.unmount()
  })

  it('says so when nothing is waiting', async () => {
    vi.mocked(myAssistant.board).mockResolvedValue({
      today: '2026-11-18',
      counts: { owner: 0, working: 0, others: 0, scheduled: 0, stalled: 0 },
      approvals: [],
      approvals_total: 0,
      groups: { owner: [], working: [], others: [], scheduled: [], stalled: [] },
      bot_id: 'b1',
      gaps: [],
    })
    const wrapper = render(AssistantBoard); await flushPromises()
    for (const g of BOARD_GROUPS) expect(wrapper.text()).toContain(t(`assistant.board.empty.${g}`))
    wrapper.unmount()
  })

  it('shows the error and retries', async () => {
    vi.mocked(myAssistant.board).mockRejectedValueOnce(new Error('加载不了'))
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.text()).toContain('加载不了')
    await (wrapper.vm as unknown as { load: () => Promise<void> }).load(); await flushPromises()
    expect(wrapper.text()).toContain('私聊 → 李四')
    wrapper.unmount()
  })
})

describe('board actions', () => {
  it('counts each group, with pending approvals under 等你, in the jump bar', async () => {
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.get('[data-test="jump-owner"]').text()).toContain(String(boardData.counts.owner + boardData.approvals_total))
    expect(wrapper.get('[data-test="jump-stalled"]').classes()).toContain('is-alert')
    expect(wrapper.get('[data-test="jump-working"]').classes()).toContain('is-zero')
    wrapper.unmount()
  })

  it('adds information, reschedules with a note and closes with a reason, then reloads', async () => {
    vi.mocked(myAssistant.supplementFollowup).mockReset().mockResolvedValue({} as never)
    vi.mocked(myAssistant.rescheduleFollowup).mockReset().mockResolvedValue({} as never)
    vi.mocked(myAssistant.closeFollowup).mockReset().mockResolvedValue({} as never)
    const wrapper = renderInline(AssistantBoard); await flushPromises()
    const submit = () => wrapper.get('[data-test="action-submit"]')
    const text = (value: string) => wrapper.get('textarea[data-test="action-text"]').setValue(value)

    await wrapper.get('[data-test="supplement-F001"]').trigger('click'); await flushPromises()
    expect(submit().attributes('disabled')).toBeDefined()
    await text('按方案 A 办'); await submit().trigger('click'); await flushPromises()
    expect(myAssistant.supplementFollowup).toHaveBeenCalledWith('F001', '按方案 A 办')

    await wrapper.get('[data-test="reschedule-F002"]').trigger('click'); await flushPromises()
    // 今天是周三 11-18：「下周一」是 11-23。
    await wrapper.get('[data-test="quick-nextMonday"]').trigger('click')
    await text('等月底结账后再看'); await submit().trigger('click'); await flushPromises()
    expect(myAssistant.rescheduleFollowup).toHaveBeenCalledWith('F002', '2026-11-23', '等月底结账后再看')

    await wrapper.get('[data-test="close-F001"]').trigger('click'); await flushPromises()
    expect(submit().attributes('disabled')).toBeDefined()
    await wrapper.get('[data-test="reason-talked"] input').setValue(true); await flushPromises()
    await submit().trigger('click'); await flushPromises()
    expect(myAssistant.closeFollowup).toHaveBeenCalledWith('F001', 'talked', undefined)
    expect(myAssistant.board).toHaveBeenCalledTimes(4)
    wrapper.unmount()
  })

  it('explains each state next to its title and all of them in the guide', async () => {
    const wrapper = renderInline(AssistantBoard); await flushPromises()
    for (const g of BOARD_GROUPS) {
      expect(wrapper.get(`[data-test="group-${g}"]`).text()).toContain(t(`assistant.board.help.${g}.short`))
      await wrapper.get(`[data-test="help-${g}"]`).trigger('click'); await flushPromises()
      expect(wrapper.get(`[data-test="help-body-${g}"]`).text()).toContain(t(`assistant.board.help.${g}.what`))
    }
    await wrapper.get('[data-test="open-guide"]').trigger('click'); await flushPromises()
    const guide = wrapper.get('[data-test="guide"]').text()
    for (const g of BOARD_GROUPS) expect(guide).toContain(t(`assistant.board.help.${g}.when`))
    expect(guide).toContain(t('assistant.board.guide.flow3'))
    wrapper.unmount()
  })

  it('shows what the assistant needs, when a scheduled item starts and why each item is where it is', async () => {
    vi.mocked(myAssistant.board).mockResolvedValue({
      ...boardData,
      groups: {
        ...boardData.groups,
        owner: [item({ reason: 'info', need: '报价上限' })],
        scheduled: [item({ id: 'F007', stage: 'scheduled', reason: 'snoozed', review_on: '2026-11-25' })],
      },
    })
    const wrapper = render(AssistantBoard); await flushPromises()
    expect(wrapper.get('[data-test="need-F001"]').text()).toBe(t('assistant.board.need', { value: '报价上限' }))
    expect(wrapper.get('[data-test="reason-F001"]').text()).toBe(t('assistant.board.reasons.info'))
    const scheduled = wrapper.get('[data-test="board-F007"]').text()
    expect(scheduled).toContain(t('assistant.board.startOn', { time: '2026-11-25' }))
    expect(scheduled).toContain(t('assistant.board.reasons.snoozed'))
    wrapper.unmount()
  })
})

describe('running', () => {
  it('shows the start of a long conclusion and expands it on request', async () => {
    const long = Array.from({ length: 8 }, (_, i) => `第 ${i + 1} 行`).join('\n')
    vi.mocked(myAssistant.jobs).mockResolvedValue({ active: [], recent: [job({ id: 'j7', status: 'done', result: long, has_result: true })] })
    const wrapper = render(AssistantJobs); await flushPromises()
    const result = () => wrapper.get('[data-test="job-j7"] pre.ma-text')
    expect(result().classes()).toContain('ma-clamp')
    expect(result().text()).toContain('第 8 行')
    await wrapper.get('[data-test="toggle-result-j7"]').trigger('click')
    expect(result().classes()).not.toContain('ma-clamp')
    wrapper.unmount()
  })

  it('shows the brief of running jobs and the conclusion of finished ones', async () => {
    const wrapper = render(AssistantJobs); await flushPromises()
    expect(wrapper.get('[data-test="job-j1"]').text()).toContain('查一下上季度的退货原因')
    expect(wrapper.get('[data-test="job-j1"]').text()).toContain(t('assistant.jobs.statuses.queued'))
    expect(wrapper.get('[data-test="job-j2"]').text()).toContain('结论在此')
    expect(wrapper.find('[data-test="cancel-j2"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('tells dispatches and platform jobs apart from sub-tasks', async () => {
    vi.mocked(myAssistant.jobs).mockResolvedValue({
      active: [
        job({ id: 'j3', kind: 'bot_help', title: '请「demo-bot」协助', origin: 'assistant', target: { id: 'b1', name: 'demo-bot' } }),
        job({ id: 'j4', title: '下班整理', origin: 'system', target: null }),
        job({ id: 'j5', origin: 'assistant', target: null }),
        job({ id: 'j6', kind: 'bot_help', origin: 'assistant', target: { id: 'b2', name: null } }),
      ],
      recent: [],
    })
    const wrapper = render(AssistantJobs); await flushPromises()
    const dispatch = wrapper.get('[data-test="job-j3"]')
    expect(dispatch.find('[data-test="dispatch-j3"]').exists()).toBe(true)
    expect(dispatch.text()).toContain(t('assistant.jobs.target', { name: 'demo-bot' }))
    expect(dispatch.find('[data-test="cancel-j3"]').exists()).toBe(true)
    expect(wrapper.get('[data-test="job-j4"]').text()).toContain(t('assistant.jobs.system'))
    expect(wrapper.find('[data-test="dispatch-j4"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="system-j5"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="dispatch-j5"]').exists()).toBe(false)
    expect(wrapper.get('[data-test="job-j6"]').text()).toContain(t('assistant.jobs.target', { name: t('assistant.jobs.targetGone') }))
    wrapper.unmount()
  })

  it('renders what a job wrote as plain text, never as markup', async () => {
    vi.mocked(myAssistant.jobs).mockResolvedValue({
      active: [],
      recent: [job({ id: 'j7', status: 'done', title: '<img src=x onerror=alert(1)>', result: '[点这里](https://evil.example.com) <script>alert(1)</script>', has_result: true })],
    })
    const wrapper = render(AssistantJobs); await flushPromises()
    const row = wrapper.get('[data-test="job-j7"]')
    expect(row.find('img').exists()).toBe(false)
    expect(row.find('script').exists()).toBe(false)
    expect(row.find('a').exists()).toBe(false)
    expect(row.text()).toContain('[点这里](https://evil.example.com)')
    wrapper.unmount()
  })

  it('cancels a running job after confirmation and reloads', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const wrapper = render(AssistantJobs); await flushPromises()
    await wrapper.get('[data-test="cancel-j1"]').trigger('click'); await flushPromises()
    expect(confirm.mock.calls[0][0]).toBe(t('assistant.jobs.cancelConfirm', { name: '退货原因' }))
    expect(myAssistant.cancelJob).toHaveBeenCalledWith('j1')
    expect(myAssistant.jobs).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('does nothing when the confirmation is dismissed', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    const wrapper = render(AssistantJobs); await flushPromises()
    await wrapper.get('[data-test="cancel-j1"]').trigger('click'); await flushPromises()
    expect(myAssistant.cancelJob).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('recently settled', () => {
  it('shows each outcome, the approver note and locked rows without content', async () => {
    const wrapper = render(AssistantRecent); await flushPromises()
    expect(wrapper.get('[data-test="settled-s1"]').text()).toContain(t('assistant.recent.statuses.executed'))
    expect(wrapper.get('[data-test="settled-s2"]').text()).toContain('语气太硬')
    expect(wrapper.get('[data-test="settled-s3"]').text()).toContain(t('assistant.page.lockedText'))
    wrapper.unmount()
  })

  it('gives every final state its own wording', async () => {
    const statuses = ['executed', 'rejected', 'withdrawn', 'failed', 'expired', 'stale'] as const
    vi.mocked(myAssistant.recentApprovals).mockResolvedValue({
      items: statuses.map((status) => settled({ id: `s-${status}`, status, executed_at: status === 'executed' ? '2026-11-12T02:00:05Z' : null, error: status === 'failed' ? 'feishu_error' : null })),
    })
    const wrapper = render(AssistantRecent); await flushPromises()
    const words = statuses.map((status) => t(`assistant.recent.statuses.${status}`))
    expect(new Set(words).size).toBe(statuses.length)
    for (const status of statuses) expect(wrapper.get(`[data-test="settled-s-${status}"]`).text()).toContain(t(`assistant.recent.statuses.${status}`))
    expect(wrapper.get('[data-test="settled-s-failed"]').text()).toContain(t('assistant.recent.error', { detail: 'feishu_error' }))
    wrapper.unmount()
  })

  it('labels settled writes staged while the owner was away', async () => {
    vi.mocked(myAssistant.recentApprovals).mockResolvedValue({
      items: [settled({ id: 's4', tool: 'systems_call', summary: '业务系统 · 库存 · 创建草稿' }), settled({ id: 's5', tool: 'cloud_call', status: 'failed', error: 'risk_changed' })],
    })
    const wrapper = render(AssistantRecent); await flushPromises()
    expect(wrapper.get('[data-test="settled-s4"]').text()).toContain(t('assistant.recent.systemsCall'))
    expect(wrapper.get('[data-test="settled-s4"]').text()).toContain('业务系统 · 库存 · 创建草稿')
    expect(wrapper.get('[data-test="settled-s5"]').text()).toContain(t('assistant.recent.cloudCall'))
    expect(wrapper.get('[data-test="settled-s5"]').text()).toContain(t('assistant.recent.error', { detail: 'risk_changed' }))
    wrapper.unmount()
  })

  it('says so when nothing has been settled', async () => {
    vi.mocked(myAssistant.recentApprovals).mockResolvedValue({ items: [] })
    const wrapper = render(AssistantRecent); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.recent.empty'))
    wrapper.unmount()
  })
})
