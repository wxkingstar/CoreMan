import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

const task = vi.hoisted(() => ({
  id: 7, code: 'T-7', title: '查上周退货率', summary: '算出上周退货率并和前一周比较', status: 'todo', source: 'web', mirror: false,
  platform: null, chat_id: null, requester_name: '李雷',
  requester: { type: 'human', id: 'u1', name: '李雷' }, principal: { type: 'human', id: 'u1', name: '李雷' },
  assignee: { type: 'bot', id: 'b1', name: '数据助手' }, reviewer: { type: 'human', id: 'u1', name: '李雷' },
  dispatcher: null, review_required: true, priority: 'normal', due_at: null,
  start_mode: 'manual', claimed_at: null, needs_drive: true,
  now: '数据助手还没开工。在飞书里对它说「做 T-7」，或给它配定时领取。',
  ask: null, can_manage: true, related_codes: [], artifacts: [],
  created_at: '2026-11-02T00:00:00Z', updated_at: '2026-11-02T00:00:00Z', closed_at: null,
}))
const asked = vi.hoisted(() => ({
  id: 8, code: 'T-8', title: '订团建场地', summary: '', status: 'todo', source: 'web', mirror: false,
  platform: null, chat_id: null, requester_name: '李雷',
  requester: { type: 'human', id: 'u1', name: '李雷' }, principal: { type: 'human', id: 'u1', name: '李雷' },
  assignee: { type: 'human', id: 'u2', name: '韩梅梅' }, reviewer: null,
  dispatcher: null, review_required: false, priority: 'urgent', due_at: null,
  start_mode: 'manual', claimed_at: null, needs_drive: false,
  now: '等韩梅梅回答：李雷把「订团建场地」交给你，接不接？',
  ask: {
    id: 'a1', kind: 'accept', to: { type: 'human', id: 'u2', name: '韩梅梅' },
    question: '李雷把「订团建场地」交给你，接不接？', why: '',
    options: [{ key: 'accept', label: '接受' }, { key: 'decline', label: '拒绝' }],
    other: true, needs_text: ['decline', 'other', 'reject'], mine: true, can_answer: true,
  },
  can_manage: true, related_codes: [], artifacts: [],
  created_at: '2026-11-02T00:00:00Z', updated_at: '2026-11-02T00:00:00Z', closed_at: null,
}))
vi.mock('@/api/workTasks', () => ({
  workTasks: {
    list: vi.fn().mockResolvedValue({ items: [task, asked], counts: { todo: 2 }, needs_me: 2 }),
    get: vi.fn().mockResolvedValue({
      ...task,
      watching: false, watchers: [], parent: null, children: [],
      events: [
        { id: 1, kind: 'created', actor_type: 'human', actor_name: '李雷', human_text: '李雷建了任务', agent_detail: { brief: '口径按退货单数除以签收单数' }, created_at: '2026-11-02T00:00:00Z' },
        { id: 2, kind: 'started', actor_type: 'bot', actor_name: '数据助手', human_text: '', agent_detail: { tool: 'query' }, created_at: '2026-11-02T00:00:00Z' },
      ],
      agent: { brief: '统计上周退货率，口径按退货单数除以签收单数', digest: '', report: '总结论：退货率 4.2%', waiting_on: null, runs: [], decision: null },
    }),
    answer: vi.fn().mockResolvedValue({ ...asked, ask: null, status: 'in_progress' }),
    stats: vi.fn().mockResolvedValue({
      days: 30, handoffs_per_task: 1.5, agent_first_pass_rate: 0.8, agent_reviewed: 5,
      by_source: [{ source: 'api', created: 3, done: 2, avg_hours: 1.2 }],
      backlog: [{ assignee: { type: 'human', id: 'u2', name: '韩梅梅' }, open: 4, overdue: 1 }],
    }),
  },
}))

import { workTasks } from '@/api/workTasks'
import { i18n } from '@/i18n'
import WorkTasksView from '@/views/WorkTasksView.vue'

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/work-tasks', name: 'work-tasks', component: WorkTasksView },
      { path: '/work-tasks/:code', name: 'work-task', component: WorkTasksView },
    ],
  })
}

describe('WorkTasksView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('opens on "needs me" and shows who has it, the start mode and the choice inline', async () => {
    const router = makeRouter()
    await router.push('/work-tasks')
    const wrapper = mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(workTasks.list).toHaveBeenCalledWith(expect.objectContaining({ view: 'needs_me' }))
    expect(wrapper.text()).toContain('T-7')
    expect(wrapper.text()).toContain(i18n.global.t('workTasks.needsDrive'))
    expect(wrapper.text()).toContain('还没开工')
    expect(wrapper.text()).toContain('数据助手（AI）')
    // 问我的事项直接在行里点。
    const ask = wrapper.find('[data-test="work-task-ask"]')
    expect(ask.text()).toContain('接不接')
    expect(ask.text()).toContain(i18n.global.t('workTasks.other'))
    await ask.find('[data-test="ask-option-accept"]').trigger('click')
    await flushPromises()
    expect(workTasks.answer).toHaveBeenCalledWith('T-8', { ask_id: 'a1', option: 'accept', text: '' })
    expect(workTasks.get).not.toHaveBeenCalled()
  })

  it('asks for a reason before declining', async () => {
    const router = makeRouter()
    await router.push('/work-tasks')
    const wrapper = mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    vi.mocked(workTasks.answer).mockClear()
    const ask = wrapper.find('[data-test="work-task-ask"]')
    await ask.find('[data-test="ask-option-decline"]').trigger('click')
    await flushPromises()
    expect(workTasks.answer).not.toHaveBeenCalled()
    expect(ask.find('[data-test="ask-submit"]').attributes('disabled')).toBeDefined()
    await ask.find('textarea[data-test="ask-text"]').setValue('这周没空')
    await ask.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()
    expect(workTasks.answer).toHaveBeenCalledWith('T-8', { ask_id: 'a1', option: 'decline', text: '这周没空' })
  })

  it('opens a task with the now line, the human history and the folded agent layer', async () => {
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
    await flushPromises()
    expect(workTasks.get).toHaveBeenCalledWith('T-7')
    const drawer = document.body.querySelector('[data-test="work-task-detail"]')?.closest('.el-drawer') ?? document.body
    expect(drawer.querySelector('[data-test="work-task-now"]')?.textContent).toContain('还没开工')
    expect(drawer.querySelector('[data-test="copy-drive"]')).not.toBeNull()
    expect(drawer.textContent).toContain('李雷建了任务')
    expect(drawer.textContent).toContain(i18n.global.t('workTasks.agentLayer'))
    // 给 Agent 看的细节默认折叠。
    const layer = drawer.querySelector('[data-test="agent-layer"]')
    expect(layer?.querySelector('.el-collapse-item.is-active')).toBeNull()
    expect(layer?.querySelector('[data-test="agent-report"]')?.textContent).toContain(i18n.global.t('workTasks.report'))
  })

  it('shows stats: backlog, sources and the first-pass rate', async () => {
    const router = makeRouter()
    await router.push('/work-tasks')
    const wrapper = mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    const tabs = wrapper.findAll('[data-test="work-task-views"] input')
    await tabs[tabs.length - 1].setValue(true)
    await flushPromises()
    expect(workTasks.stats).toHaveBeenCalledWith(30)
    const panel = wrapper.find('[data-test="work-task-stats"]')
    expect(panel.text()).toContain('韩梅梅')
    expect(panel.text()).toContain(i18n.global.t('workTasks.sources.api'))
  })
})
