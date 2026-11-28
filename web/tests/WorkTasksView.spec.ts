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
      related_codes: ['T-5'],
      linked: { 'T-5': { title: '算上上周退货率', status: 'done' }, 'T-9': { title: '拉退货明细', status: 'in_progress' } },
      watching: false, watchers: [], parent: null,
      children: [{ ...task, code: 'T-9', title: '拉退货明细', status: 'in_progress', now: '数据助手在拉明细' }],
      overview: {
        state: 'need_human', steps_done: 1, steps_total: 3,
        asks: [{ ...asked.ask, code: 'T-7-Q1', title: '问：退货率按单数还是按金额算？', question: '退货率按单数还是按金额算？', options: [{ key: 'o1', label: '按单数' }], mine: true, can_answer: true }],
        doing: [{
          code: 'T-9', title: '拉退货明细', status: 'in_progress', kind: 'running', question: false,
          assignee: { type: 'bot', id: 'b1', name: '数据助手' }, since: '2026-11-02T00:00:00Z', closed_at: null,
          note: '明细拉了一半', note_at: '2026-11-02T00:05:00Z',
        }],
        plan: ['和前一周比较'],
        done: [{
          code: 'T-6', title: '确认口径', status: 'done', kind: 'done', question: false,
          assignee: { type: 'bot', id: 'b1', name: '数据助手' }, since: '2026-11-02T00:00:00Z', closed_at: '2026-11-02T00:04:00Z',
          note: '口径按退货单数除以签收单数', note_at: null,
        }],
        followups: [], decisions: [], stalled: null,
        latest: { text: '口径已定，正在拉明细', at: '2026-11-02T00:06:00Z', actor_name: '数据助手' },
      },
      events: [
        { id: 1, kind: 'created', actor_type: 'human', actor_name: '李雷', human_text: '李雷建了任务', agent_detail: { brief: '口径按退货单数除以签收单数' }, created_at: '2026-11-02T00:00:00Z', milestone: true },
        { id: 3, kind: 'subtask', actor_type: 'bot', actor_name: '数据助手', human_text: '拆出 T-9：拉退货明细', agent_detail: {}, created_at: '2026-11-02T00:00:00Z', milestone: false },
        { id: 2, kind: 'started', actor_type: 'bot', actor_name: '数据助手', human_text: '', agent_detail: { tool: 'query' }, created_at: '2026-11-02T00:00:00Z', milestone: false },
      ],
      agent: { brief: '统计上周退货率，口径按退货单数除以签收单数', digest: '', report: '总结论：退货率 4.2%', waiting_on: null, runs: [], decision: null },
    }),
    markdown: vi.fn().mockResolvedValue('# T-7 查上周退货率\n\n- 状态：待开始\n'),
    answer: vi.fn().mockResolvedValue({ ...asked, ask: null, status: 'in_progress' }),
    start: vi.fn(),
    followup: vi.fn(),
    create: vi.fn(),
    stats: vi.fn().mockResolvedValue({
      days: 30, handoffs_per_task: 1.5, agent_first_pass_rate: 0.8, agent_reviewed: 5,
      by_source: [
        { source: 'web', record: false, created: 4, done: 3, avg_hours: 2.5 },
        { source: 'api', record: true, created: 3, done: 2, avg_hours: 1.2 },
      ],
      backlog: [{ assignee: { type: 'human', id: 'u2', name: '韩梅梅' }, open: 4, overdue: 1 }],
    }),
  },
}))

import { workTasks } from '@/api/workTasks'
import { installPageMemory } from '@/composables/usePageMemory'
import { i18n } from '@/i18n'
import WorkTaskCreateDialog from '@/components/workTasks/WorkTaskCreateDialog.vue'
import WorkTaskDetailView from '@/views/WorkTaskDetailView.vue'
import WorkTasksView from '@/views/WorkTasksView.vue'

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/work-tasks', name: 'work-tasks', component: WorkTasksView },
      { path: '/work-tasks/:code', name: 'work-task', component: WorkTaskDetailView },
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
    // AI 员工名字后面是小图标（指上去提示是 AI），不再写「（AI）」。
    expect(wrapper.text()).toContain('数据助手')
    expect(wrapper.text()).not.toContain('（AI）')
    expect(wrapper.find('[data-test="ai-mark"]').attributes('aria-label')).toBe(i18n.global.t('workTasks.aiHint'))
    // 问我的事项直接在行里点。
    const ask = wrapper.find('[data-test="work-task-ask"]')
    expect(ask.text()).toContain('接不接')
    expect(ask.text()).toContain(i18n.global.t('workTasks.other'))
    await ask.find('[data-test="ask-option-accept"]').trigger('click')
    await flushPromises()
    expect(workTasks.answer).toHaveBeenCalledWith('T-8', { ask_id: 'a1', option: 'accept', text: '' })
    expect(workTasks.get).not.toHaveBeenCalled()
  })

  it('shows what I started under what needs me, and keeps automatic records out unless asked', async () => {
    const going = { ...task, id: 12, code: 'T-12', title: '对接仓储系统', status: 'in_progress', needs_drive: false, now: '数据助手处理中' }
    // 「待我处理」一次、「我发起且还在进行的」一次：后一次里已经在上面列过的不再重复。
    vi.mocked(workTasks.list).mockImplementation(async (params = {}) => (
      params.unfinished
        ? { items: [task, going], counts: {}, needs_me: 2 }
        : { items: [task, asked], counts: { todo: 2 }, needs_me: 2, upcoming: [{ code: 'T-12', title: '对接仓储系统', step: '请李雷同意合并' }] }
    ) as never)
    const router = makeRouter()
    await router.push('/work-tasks')
    const wrapper = mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(workTasks.list).toHaveBeenCalledWith(expect.objectContaining({ view: 'created', unfinished: true }))
    const mine = wrapper.find('[data-test="tasks-mine"]')
    expect(mine.find('h4').text()).toBe(i18n.global.t('workTasks.myOpen'))
    expect(mine.text()).toContain('对接仓储系统')
    expect(mine.text()).not.toContain('查上周退货率')
    // 后面的步骤点到我名字的事：提前列出来，点了进那件事。
    const coming = wrapper.find('[data-test="tasks-upcoming"]')
    expect(coming.text()).toContain('请李雷同意合并')
    expect(coming.text()).toContain('对接仓储系统')
    // 换到「全部」：只剩一张表，默认不带自动记录，勾上才带，并记在地址里。
    const tabs = wrapper.findAll('[data-test="work-task-views"] input')
    await tabs[3].setValue(true)
    await flushPromises()
    expect(wrapper.find('[data-test="tasks-mine"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="tasks-upcoming"]').exists()).toBe(false)
    expect(vi.mocked(workTasks.list).mock.lastCall?.[0]).toMatchObject({ view: 'all', records: undefined })
    await wrapper.find('[data-test="show-records"] input').setValue(true)
    await flushPromises()
    expect(vi.mocked(workTasks.list).mock.lastCall?.[0]).toMatchObject({ view: 'all', records: true })
    expect(router.currentRoute.value.query.records).toBe('1')
    vi.mocked(workTasks.list).mockResolvedValue({ items: [task, asked], counts: { todo: 2 }, needs_me: 2 } as never)
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

  it('opens a task on the whole picture, with the human history and the folded agent layer below', async () => {
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    const wrapper = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
    await flushPromises()
    expect(workTasks.get).toHaveBeenCalledWith('T-7')
    const drawer = wrapper.element
    expect(drawer.querySelector('[data-test="back-to-list"]')).not.toBeNull()
    // 首屏是全貌：状态与进度、最新一句、目标、要你定的、正在做、接下来、已完成。
    const overview = wrapper.find('[data-test="work-task-overview"]')
    expect(overview.find('[data-test="overview-state"]').text()).toBe(i18n.global.t('workTasks.overview.states.need_human'))
    expect(overview.find('[data-test="overview-progress"]').text()).toBe(i18n.global.t('workTasks.overview.progress', { done: 1, total: 3 }))
    expect(overview.find('[data-test="overview-latest"]').text()).toContain('口径已定，正在拉明细')
    expect(overview.find('[data-test="overview-goal"]').text()).toContain('算出上周退货率并和前一周比较')
    expect(overview.find('[data-test="overview-asks"]').text()).toContain(i18n.global.t('workTasks.overview.needYou'))
    expect(overview.find('[data-test="overview-plan"]').text()).toContain('和前一周比较')
    expect(overview.find('[data-test="overview-done"]').text()).toContain('口径按退货单数除以签收单数')
    expect(overview.find('[data-test="overview-done"]').text()).toContain(i18n.global.t('workTasks.overview.took', { d: i18n.global.t('workTasks.overview.durMin', { m: 4 }) }))
    // 有问题要定时，「现在」那句只是重复，不再写；标题下面一行写谁在做、谁要的。
    expect(drawer.querySelector('[data-test="work-task-now"]')).toBeNull()
    expect(wrapper.find('[data-test="work-task-meta"]').text()).toContain('数据助手')
    expect(wrapper.find('[data-test="work-task-meta"]').text()).toContain(i18n.global.t('workTasks.needsDrive'))
    // 下面某一步的问题在这一页就能答，答完重新取这一页。
    vi.mocked(workTasks.get).mockClear()
    await overview.find('[data-test="ask-option-o1"]').trigger('click')
    await flushPromises()
    expect(workTasks.answer).toHaveBeenCalledWith('T-7-Q1', { ask_id: 'a1', option: 'o1', text: '' })
    expect(workTasks.get).toHaveBeenCalledWith('T-7')
    // 属性表收在「任务信息」里，默认折叠。
    expect(drawer.querySelector('[data-test="task-info"] .el-collapse-item.is-active')).toBeNull()
    // 执行人、发起人、代表人、验收人这些名词都能指上去看解释，三种语言都写了。
    const terms = [...drawer.querySelectorAll('[data-test="task-info"] [data-test="task-term"]')].map(el => el.getAttribute('data-term'))
    expect(terms).toEqual(expect.arrayContaining(['assignee', 'requester', 'principal', 'reviewer']))
    for (const locale of ['zh', 'en', 'ja'] as const) {
      const defined = i18n.global.getLocaleMessage(locale).workTasks.terms as Record<string, { name: string; body: string }>
      for (const term of ['assignee', 'requester', 'principal', 'reviewer', 'dispatcher', 'needsDrive', 'autoStart', 'claimed']) {
        expect(defined[term].name).toBeTruthy()
        expect(defined[term].body.length).toBeGreaterThan(10)
      }
    }
    // 进度条一步一格：做完 1、在做 1、还没开始 1。
    expect(overview.findAll('.segment').map(el => el.classes().filter(c => c !== 'segment')[0])).toEqual(['done', 'doing', 'todo'])
    expect(drawer.textContent).toContain('李雷建了任务')
    expect(drawer.textContent).toContain(i18n.global.t('workTasks.agentLayer'))
    // 给 Agent 看的细节默认折叠。
    const layer = drawer.querySelector('[data-test="agent-layer"]')
    expect(layer?.querySelector('.el-collapse-item.is-active')).toBeNull()
    expect(layer?.querySelector('[data-test="agent-report"]')?.textContent).toContain(i18n.global.t('workTasks.report'))
    // 事件里给 AI 的细节不出现在给人看的经过里；经过默认只有里程碑，流水点「显示全部」才出来。
    expect(drawer.querySelector('[data-test="work-task-timeline"]')?.textContent).not.toContain('口径按退货单数')
    expect(drawer.querySelector('[data-test="work-task-timeline"]')?.textContent).not.toContain('拆出')
    await wrapper.find('[data-test="all-events"]').trigger('click')
    expect(drawer.querySelector('[data-test="work-task-timeline"]')?.textContent).toContain('拆出 T-9')
    // 正在做的一步：谁在做、几点开始、最近一次进展；点了进那一步。
    const doing = wrapper.find('[data-test="overview-doing"]')
    expect(doing.text()).toContain('拉退货明细')
    expect(doing.text()).toContain('数据助手')
    expect(doing.text()).toContain('明细拉了一半')
    await doing.find('[data-test="overview-step"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.params.code).toBe('T-9')
    wrapper.unmount()
  })

  it('keeps the now line and the start command for a task with nothing under it', async () => {
    const full = await vi.mocked(workTasks.get)('T-7')
    vi.mocked(workTasks.get).mockResolvedValueOnce({
      ...full,
      children: [],
      overview: { state: 'need_human', steps_done: 0, steps_total: 0, asks: [], doing: [], plan: [], done: [], followups: [], decisions: [], stalled: null, latest: null },
    })
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    // 挂到文档上：一栏时操作按钮挪到全貌下面的那个位置，要在文档里才找得到。
    const wrapper = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
    await flushPromises()
    expect(wrapper.find('[data-test="work-task-now"]').text()).toContain('还没开工')
    expect(wrapper.find('[data-test="copy-drive"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="overview-progress"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="overview-doing"]').exists()).toBe(false)
    // 页面上直接点「开工」，不用去飞书里说一句。
    const started = { ...full, children: [], needs_drive: false, status: 'waiting', now: '排队等数据助手开始', overview: { state: 'running', steps_done: 0, steps_total: 0, asks: [], doing: [], plan: [], done: [], followups: [], decisions: [], stalled: null, latest: null } }
    vi.mocked(workTasks.start).mockResolvedValueOnce(started as never)
    const button = wrapper.find('[data-test="start-task"]')
    expect(button.text()).toBe(i18n.global.t('workTasks.start'))
    await button.trigger('click')
    await flushPromises()
    expect(workTasks.start).toHaveBeenCalledWith('T-7')
    expect(wrapper.find('[data-test="work-task-now"]').text()).toContain('排队等数据助手开始')
    expect(wrapper.find('[data-test="start-task"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('groups steps by phase, folds older finished steps and flags a step that is not moving', async () => {
    const full = await vi.mocked(workTasks.get)('T-7')
    const step = (code: string, title: string, phase: string, extra: Record<string, unknown> = {}) => ({
      code, title, status: 'done', kind: 'done', question: false, assignee: { type: 'bot', id: 'b1', name: '数据助手' },
      since: '2026-11-02T00:00:00Z', closed_at: '2026-11-02T00:04:00Z', note: '', note_at: null, phase, stalled: null, ...extra,
    })
    vi.mocked(workTasks.get).mockResolvedValueOnce({
      ...full,
      overview: {
        ...full.overview, asks: [], plan: [],
        doing: [step('T-30', '回传发货', '发货联调', {
          status: 'waiting', kind: 'running', closed_at: null, stalled: { kind: 'queued', since: '2026-11-02T00:00:00Z' },
        })],
        done: [
          step('T-21', '盘点约定', ''), step('T-22', '推退货单', '退货联调'), step('T-23', '退货收货', '退货联调'),
          step('T-24', '核对退货', '退货联调'), step('T-25', '建寄售单', '寄售联调'), step('T-26', '寄售借出', '寄售联调'),
          step('T-27', '寄售叫回', '寄售联调'),
        ],
      },
    } as never)
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    const wrapper = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
    await flushPromises()
    // 排了很久没开始：标出来，不能看着像在做。
    expect(wrapper.find('[data-test="step-stalled"]').text()).toContain('还没开始')
    expect(wrapper.find('[data-test="overview-doing"] .phase').text()).toBe('发货联调')
    // 做完七步：只列最近五步，更早的两步点一下再展开；同一阶段的排在一起，前面一行小标题。
    const done = wrapper.find('[data-test="overview-done"]')
    expect(done.findAll('[data-test="overview-step"]')).toHaveLength(5)
    expect(done.find('[data-test="done-more"]').text()).toBe(i18n.global.t('workTasks.overview.earlier', { n: 2 }))
    expect(done.findAll('.phase').map(el => el.text())).toEqual(['退货联调', '寄售联调'])
    await done.find('[data-test="done-more"]').trigger('click')
    expect(done.findAll('[data-test="overview-step"]')).toHaveLength(7)
    expect(done.findAll('.phase').map(el => el.text())).toEqual([i18n.global.t('workTasks.overview.noPhase'), '退货联调', '寄售联调'])
    wrapper.unmount()
  })

  it('lets people tick off what is left after delivery and lists what was decided', async () => {
    const full = await vi.mocked(workTasks.get)('T-7')
    const done = {
      ...full, status: 'done', needs_drive: false, summary: '上周退货率 4.2%', now: '已完成：上周退货率 4.2%',
      overview: {
        ...full.overview, state: 'done', asks: [], doing: [], plan: [], latest: null,
        followups: [
          { index: 0, text: '把结论发给财务', done: false, by: '', at: null, task: '' },
          { index: 1, text: '更新周报模板', done: true, by: '韩梅梅', at: '2026-11-02T01:00:00Z', task: '' },
        ],
        decisions: [{ code: 'T-7-Q1', question: '退货率按单数还是按金额算？', by: '李雷', at: '2026-11-02T00:03:00Z', answer: '按单数' }],
      },
    }
    vi.mocked(workTasks.get).mockResolvedValueOnce(done as never)
    vi.mocked(workTasks.followup).mockResolvedValue(done as never)
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    const wrapper = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
    await flushPromises()
    // 状态里只算还没办的那一件。
    expect(wrapper.find('[data-test="overview-state"]').text()).toBe(i18n.global.t('workTasks.overview.doneWithFollowups', { n: 1 }))
    const rows = wrapper.findAll('[data-test="overview-followup"]')
    expect(rows[1].classes()).toContain('ticked')
    expect(rows[1].text()).toContain(i18n.global.t('workTasks.overview.doneBy', { name: '韩梅梅' }))
    await rows[0].find('[data-test="followup-tick"]').trigger('click')
    await flushPromises()
    expect(workTasks.followup).toHaveBeenCalledWith('T-7', 0, { done: true })
    await rows[0].find('[data-test="followup-task"]').trigger('click')
    await flushPromises()
    expect(workTasks.followup).toHaveBeenCalledWith('T-7', 0, { as_task: true })
    // 定过的事集中一栏：问题、谁定的、答了什么。
    const decided = wrapper.find('[data-test="overview-decisions"]')
    expect(decided.text()).toContain('退货率按单数还是按金额算？')
    expect(decided.text()).toContain('按单数')
    expect(decided.text()).toContain('李雷')
    wrapper.unmount()
  })

  it('calls the one-liner the result once the work is delivered and waiting for review', async () => {
    const full = await vi.mocked(workTasks.get)('T-7')
    vi.mocked(workTasks.get).mockResolvedValueOnce({
      ...full, status: 'review', needs_drive: false, summary: '上周退货率 4.2%，比前一周低 0.3 个点', now: '等李雷验收',
      overview: { ...full.overview, asks: [], doing: [], plan: [] },
    })
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    const wrapper = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    const goal = wrapper.find('[data-test="overview-goal"]')
    expect(goal.find('h4').text()).toBe(i18n.global.t('workTasks.overview.result'))
    // 结果是这时最要紧的一句；交付前的那句进展不再摆在上面。
    expect(goal.find('p.lead').text()).toContain('上周退货率 4.2%')
    expect(wrapper.find('[data-test="overview-latest"]').exists()).toBe(false)
  })

  it('starts a new task for an AI employee right away unless told to leave it', async () => {
    const created = await vi.mocked(workTasks.get)('T-7')
    vi.mocked(workTasks.create).mockResolvedValue({ ...created, start_error: '请他先私聊「数据助手」一次' } as never)
    const wrapper = mount(WorkTaskCreateDialog, {
      props: { modelValue: true },
      global: { plugins: [ElementPlus, i18n], stubs: { AssigneePicker: { template: '<button data-test="pick-bot" @click="$emit(\'update:modelValue\', { type: \'bot\', id: \'b1\' })" />' } } },
      attachTo: document.body,
    })
    await flushPromises()
    const body = (): Element => document.body
    ;(body().querySelector('[data-test="create-title"] input, input[data-test="create-title"]') as HTMLInputElement).value = '查上周退货率'
    body().querySelector('[data-test="create-title"] input, input[data-test="create-title"]')!.dispatchEvent(new Event('input'))
    ;(body().querySelector('[data-test="pick-bot"]') as HTMLElement).click()
    await flushPromises()
    // 交给 AI 员工：默认现在开工，说明写清以谁的身份做、结果发到哪。
    expect(body().querySelector('.create-hint')?.textContent).toContain(i18n.global.t('workTasks.startNowHint'))
    ;(body().querySelector('[data-test="create-submit"]') as HTMLElement).click()
    await flushPromises()
    expect(vi.mocked(workTasks.create).mock.lastCall?.[0]).toMatchObject({ title: '查上周退货率', start: true })
    // 没开成：任务照样建好，原因提示给人。
    expect(document.body.textContent).toContain('请他先私聊「数据助手」一次')
    wrapper.unmount()
  })

  it('keeps an open task page fresh without a manual reload', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      const full = await vi.mocked(workTasks.get)('T-7')
      const router = makeRouter()
      await router.push('/work-tasks/T-7')
      const wrapper = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
      await flushPromises()
      vi.mocked(workTasks.get).mockClear()
      vi.mocked(workTasks.get).mockResolvedValueOnce({
        ...full,
        overview: { ...full.overview, latest: { text: '明细拉完了，正在算退货率', at: '2026-11-02T00:09:00Z', actor_name: '数据助手' } },
      })
      // 每 15 秒悄悄取一次：页面上换成最新的进展，不闪加载圈。
      vi.advanceTimersByTime(15_000)
      await flushPromises()
      expect(workTasks.get).toHaveBeenCalledWith('T-7')
      expect(wrapper.find('[data-test="overview-latest"]').text()).toContain('明细拉完了')
      // 已经结束的任务不再取。
      vi.mocked(workTasks.get).mockResolvedValueOnce({ ...full, status: 'done' })
      vi.advanceTimersByTime(15_000)
      await flushPromises()
      vi.mocked(workTasks.get).mockClear()
      vi.advanceTimersByTime(30_000)
      await flushPromises()
      expect(workTasks.get).not.toHaveBeenCalled()
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('gives AI a plain-text page with ?format=md', async () => {
    const router = makeRouter()
    await router.push('/work-tasks/T-7?format=md')
    const wrapper = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(workTasks.markdown).toHaveBeenCalledWith('T-7')
    expect(wrapper.find('[data-test="work-task-plain"]').text()).toContain('# T-7 查上周退货率')
    expect(wrapper.find('[data-test="work-task-detail"]').exists()).toBe(false)
  })

  it('goes back to the list from a task opened by link', async () => {
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    const wrapper = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    await wrapper.find('[data-test="back-to-list"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('work-tasks')
  })

  it('links task codes in related and in the text, each with its status icon', async () => {
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    const page = mount(WorkTaskDetailView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
    await flushPromises()
    const links = [...document.body.querySelectorAll<HTMLAnchorElement>('[data-test="task-code"]')]
    const related = links.find(a => a.textContent?.includes('T-5'))
    expect(related?.getAttribute('href')).toBe('/work-tasks/T-5')
    expect(related?.querySelector('[data-status="done"]')).not.toBeNull()
    // 时间线里提到的子任务可点，进行中的转圈。
    const mentioned = links.find(a => a.textContent?.includes('T-9'))
    expect(mentioned?.getAttribute('href')).toBe('/work-tasks/T-9')
    expect(mentioned?.querySelector('.is-loading[data-status="in_progress"]')).not.toBeNull()
    expect(mentioned?.getAttribute('title')).toContain('拉退货明细')
    // 任务自己的编号不变成链接。
    expect(links.some(a => a.textContent?.trim() === 'T-7')).toBe(false)
    page.unmount()
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
    // 自动记录单独一张表，不和人建的事混在一起。
    const records = panel.find('[data-test="stats-records"]')
    expect(records.text()).toContain(i18n.global.t('workTasks.sources.api'))
    expect(records.text()).not.toContain(i18n.global.t('workTasks.sources.web'))
  })

  it('opens a task on its own page and keeps the chosen tab in the address', async () => {
    const router = makeRouter()
    await router.push('/work-tasks')
    const wrapper = mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(wrapper.text()).toContain(i18n.global.t('workTasks.created'))
    const tabs = wrapper.findAll('[data-test="work-task-views"] input')
    await tabs[1].setValue(true)
    await flushPromises()
    expect(vi.mocked(workTasks.list).mock.lastCall?.[0]).toMatchObject({ view: 'created' })
    expect(router.currentRoute.value.query.view).toBe('created')
    await wrapper.find('.tasks-table .el-table__row').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('work-task')
    expect(router.currentRoute.value.params.code).toBe('T-7')
    wrapper.unmount()
  })

  it('reopens on the tab kept in the address', async () => {
    const router = makeRouter()
    await router.push('/work-tasks?view=watching')
    mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(vi.mocked(workTasks.list).mock.lastCall?.[0]).toMatchObject({ view: 'watching' })
  })

  it('comes back to where it was: the opened layers stay open and the clicked task flashes', async () => {
    sessionStorage.clear()
    const router = makeRouter()
    installPageMemory(router)
    await router.push('/work-tasks/T-7')
    const wrapper = mount(
      { template: '<router-view />' },
      { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body },
    )
    await flushPromises()
    await wrapper.find('[data-test="all-events"]').trigger('click')
    // 点「正在做」里的一步进 T-9。
    await wrapper.find('[data-test="overview-doing"] [data-test="overview-step"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.params.code).toBe('T-9')
    // 按返回：先有 popstate，再导航。
    window.dispatchEvent(new PopStateEvent('popstate'))
    router.back()
    await flushPromises()
    await new Promise(r => setTimeout(r, 200))
    expect(router.currentRoute.value.params.code).toBe('T-7')
    expect(wrapper.find('[data-test="work-task-timeline"]').text()).toContain('拆出 T-9')
    expect(wrapper.find('[data-test="overview-doing"] .cm-flash[data-test="overview-step"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
