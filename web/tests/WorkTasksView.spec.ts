import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

const task = vi.hoisted(() => ({
  id: 7, code: 'T-7', title: '查上周退货率', summary: '算出上周退货率并和前一周比较', status: 'shadow',
  platform: 'feishu', chat_id: 'oc_1', requester_name: '李雷',
  assignee: { id: 'b1', name: '数据助手' }, dispatcher: { id: 'b0', name: '调度员' }, related_codes: [],
  created_at: '2026-11-02T00:00:00Z', updated_at: '2026-11-02T00:00:00Z', closed_at: null,
}))
vi.mock('@/api/workTasks', () => ({
  workTasks: {
    list: vi.fn().mockResolvedValue({ items: [task], counts: { shadow: 1 } }),
    get: vi.fn().mockResolvedValue({
      ...task,
      events: [{ id: 1, kind: 'created', actor_type: 'bot', actor_name: '调度员', human_text: '试运行：调度员会交给数据助手', agent_detail: { reason: '数据问题' }, created_at: '2026-11-02T00:00:00Z' }],
      agent: { brief: '统计上周退货率，口径按退货单数除以签收单数', digest: '', decision: { id: 3, probability: 0.92, outcome: 'dispatched', recent_messages: [{ sender: '李雷', sender_kind: 'human', text: '上周退货率多少？' }], dispatch: { plan: { action: 'create' } }, created_at: '2026-11-02T00:00:00Z' } },
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

  it('lists tasks with the human-facing fields', async () => {
    const router = makeRouter()
    await router.push('/work-tasks')
    const wrapper = mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] } })
    await flushPromises()
    expect(wrapper.text()).toContain('T-7')
    expect(wrapper.text()).toContain('查上周退货率')
    expect(wrapper.text()).toContain('数据助手')
    expect(workTasks.get).not.toHaveBeenCalled()
  })

  it('opens a task by code with the timeline and the folded agent layer', async () => {
    const router = makeRouter()
    await router.push('/work-tasks/T-7')
    mount(WorkTasksView, { global: { plugins: [ElementPlus, i18n, router] }, attachTo: document.body })
    await flushPromises()
    expect(workTasks.get).toHaveBeenCalledWith('T-7')
    const drawer = document.body.querySelector('[data-test="work-task-detail"]')?.closest('.el-drawer') ?? document.body
    expect(drawer.textContent).toContain('试运行：调度员会交给数据助手')
    expect(drawer.textContent).toContain(i18n.global.t('workTasks.agentLayer'))
  })
})
