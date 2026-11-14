import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Component } from 'vue'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { attention: vi.fn(), jobs: vi.fn(), cancelJob: vi.fn(), recentApprovals: vi.fn() },
}))

import { myAssistant, type AssistantJob, type Attention, type SettledApproval } from '@/api/myAssistant'
import AssistantAttention from '@/components/assistant/AssistantAttention.vue'
import AssistantJobs from '@/components/assistant/AssistantJobs.vue'
import AssistantRecent from '@/components/assistant/AssistantRecent.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const job = (over: Partial<AssistantJob>): AssistantJob => ({
  id: 'j1', kind: 'subtask', status: 'queued', title: '退货原因', rounds: 1, created_at: '2026-11-13T01:00:00Z',
  finished_at: null, has_result: false, brief: '查一下上季度的退货原因', result: null, blocked_reason: null, ...over,
})
const attention: Attention = {
  today: '2026-11-13',
  approvals: [
    { id: 'a1', tool: 'feishu_im_send_message', summary: '私聊 → 李四', created_at: '2026-11-13T01:00:00Z', link: 'https://tower.example.com' },
    { id: 'a2', tool: 'feishu_doc_update', summary: null, created_at: '2026-11-13T02:00:00Z', link: null },
  ],
  jobs: [job({ id: 'j9', status: 'blocked', title: '合同条款', blocked_reason: '需要你确认范围' })],
  followups: [{ id: 'F003', category: 'remind', status: 'open', title: '续约', review_on: null, due_on: '2026-11-13', origin_message_id: null, created_at: '2026-11-01T00:00:00Z' }],
}
const settled = (over: Partial<SettledApproval>): SettledApproval => ({
  id: 's1', tool: 'feishu_im_send_message', channel: 'p2p', status: 'executed', summary: '私聊 → 李四', reason: null, error: null,
  created_at: '2026-11-12T01:00:00Z', decided_at: '2026-11-12T02:00:00Z', executed_at: '2026-11-12T02:00:05Z', locked: false, ...over,
})
const render = (component: Component) => mount(component, { global: { plugins: [ElementPlus, i18n] } })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(myAssistant.attention).mockReset().mockResolvedValue(attention)
  vi.mocked(myAssistant.jobs).mockReset().mockResolvedValue({ active: [job({})], recent: [job({ id: 'j2', status: 'done', title: '已完成的调查', result: '结论在此', finished_at: '2026-11-12T03:00:00Z', has_result: true })] })
  vi.mocked(myAssistant.cancelJob).mockReset().mockResolvedValue({ id: 'j1', status: 'cancelled' })
  vi.mocked(myAssistant.recentApprovals).mockReset().mockResolvedValue({ items: [settled({}), settled({ id: 's2', status: 'rejected', reason: '语气太硬' }), settled({ id: 's3', status: 'withdrawn', summary: null, locked: true })] })
})

describe('needs you', () => {
  it('shows pending approvals with a safe link, blocked sub-tasks and due follow-ups', async () => {
    const wrapper = render(AssistantAttention); await flushPromises()
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
    expect(wrapper.text()).toContain('合同条款')
    expect(wrapper.text()).toContain('需要你确认范围')
    expect(wrapper.text()).toContain('F003')
    expect(wrapper.text()).toContain('续约')
    wrapper.unmount()
  })

  it('marks a dispatch that is waiting for approval', async () => {
    vi.mocked(myAssistant.attention).mockResolvedValue({
      ...attention,
      approvals: [{ id: 'a3', tool: 'assistant_ask_bot', summary: '调度员工 → demo-bot', created_at: '2026-11-13T03:00:00Z', link: null }],
    })
    const wrapper = render(AssistantAttention); await flushPromises()
    const row = wrapper.get('[data-test="approval-a3"]')
    expect(row.text()).toContain(t('assistant.attention.dispatch'))
    expect(row.text()).toContain('调度员工 → demo-bot')
    wrapper.unmount()
  })

  it('labels writes staged while the owner was away with the system and operation', async () => {
    vi.mocked(myAssistant.attention).mockResolvedValue({
      ...attention,
      approvals: [
        { id: 'a4', tool: 'systems_call', summary: '业务系统 · 库存 · 创建草稿', created_at: '2026-11-13T03:00:00Z', link: null },
        { id: 'a5', tool: 'cloud_call', summary: '云服务 · slb · AddAccessControlListEntry', created_at: '2026-11-13T03:00:00Z', link: null },
      ],
    })
    const wrapper = render(AssistantAttention); await flushPromises()
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
    vi.mocked(myAssistant.attention).mockResolvedValue({ today: '2026-11-13', approvals: [], jobs: [], followups: [] })
    const wrapper = render(AssistantAttention); await flushPromises()
    for (const key of ['approvalsEmpty', 'jobsEmpty', 'followupsEmpty']) expect(wrapper.text()).toContain(t(`assistant.attention.${key}`))
    wrapper.unmount()
  })

  it('shows the error and retries', async () => {
    vi.mocked(myAssistant.attention).mockRejectedValueOnce(new Error('加载不了'))
    const wrapper = render(AssistantAttention); await flushPromises()
    expect(wrapper.text()).toContain('加载不了')
    await (wrapper.vm as unknown as { load: () => Promise<void> }).load(); await flushPromises()
    expect(wrapper.text()).toContain('私聊 → 李四')
    wrapper.unmount()
  })
})

describe('running', () => {
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
