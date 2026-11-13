import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { accessLog: vi.fn() },
}))

import { myAssistant, type AccessEntry } from '@/api/myAssistant'
import AssistantAccessLog from '@/components/assistant/AssistantAccessLog.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const entry = (over: Partial<AccessEntry>): AccessEntry => ({
  id: 9, at: '2026-11-13T02:00:00Z', actor_kind: 'owner', actor_name: '张三', action: 'notes.read', target: 'page', ...over,
})
const render = () => mount(AssistantAccessLog, { global: { plugins: [ElementPlus, i18n] } })
// 后端 coreman/core/assistants/access_log.py 的 ACTIONS：那边加了动作，这里和文案要一起加。
const ACTIONS = [
  'approvals.read', 'followups.read', 'jobs.read', 'notes.index', 'notes.read', 'settings.read',
  'chatlogs.read', 'tasks.read', 'import.preview', 'import.notes', 'export',
  'cli.rewrap', 'cli.chatlogs', 'cli.tasks', 'cli.session',
]

beforeEach(() => {
  vi.mocked(myAssistant.accessLog).mockReset().mockResolvedValue({
    items: [
      entry({ id: 12, actor_kind: 'system', actor_name: null, action: 'cli.rewrap', target: '' }),
      entry({ id: 11, actor_kind: 'admin', actor_name: '李四', action: 'chatlogs.read', target: 'rows=2' }),
      entry({ id: 10 }),
      entry({ id: 9, action: 'something.new', target: '' }),
    ],
    next_before: null,
  })
})

describe('access log', () => {
  it('shows when, who and what, and flags access by anyone but the owner', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(t('assistant.settings.accessLogHint'))
    expect(wrapper.get('[data-test="access-foreign-0"]').text()).toBe(t('assistant.settings.accessWho.system'))
    expect(wrapper.get('[data-test="access-0"]').text()).toContain(t('assistant.settings.accessActions.cli_rewrap'))
    expect(wrapper.get('[data-test="access-foreign-1"]').text()).toBe(t('assistant.settings.accessWho.admin', { name: '李四' }))
    expect(wrapper.get('[data-test="access-1"]').text()).toContain('rows=2')
    expect(wrapper.find('[data-test="access-foreign-2"]').exists()).toBe(false)
    const mine = wrapper.get('[data-test="access-2"]').text()
    expect(mine).toContain(t('assistant.settings.accessWho.owner'))
    expect(mine).toContain(t('assistant.settings.accessActions.notes_read'))
    expect(wrapper.find('[data-test="access-more"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('falls back to the action code when it has no label', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.get('[data-test="access-3"]').text()).toContain('something.new')
    wrapper.unmount()
  })

  it('loads earlier entries on demand', async () => {
    vi.mocked(myAssistant.accessLog)
      .mockResolvedValueOnce({ items: [entry({ id: 30 }), entry({ id: 29 })], next_before: 29 })
      .mockResolvedValueOnce({ items: [entry({ id: 28 })], next_before: null })
    const wrapper = render(); await flushPromises()
    expect(myAssistant.accessLog).toHaveBeenLastCalledWith(undefined)
    await wrapper.get('[data-test="access-more"]').trigger('click'); await flushPromises()
    expect(myAssistant.accessLog).toHaveBeenLastCalledWith(29)
    expect(wrapper.findAll('li')).toHaveLength(3)
    expect(wrapper.find('[data-test="access-more"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says so when nothing has been recorded', async () => {
    vi.mocked(myAssistant.accessLog).mockResolvedValue({ items: [], next_before: null })
    const wrapper = render(); await flushPromises()
    expect(wrapper.get('[data-test="access-empty"]').text()).toBe(t('assistant.settings.accessEmpty'))
    wrapper.unmount()
  })

  it('has a label for every action in all three languages', () => {
    for (const locale of ['zh', 'en', 'ja'] as const) {
      for (const action of ACTIONS) {
        const key = 'assistant.settings.accessActions.' + action.replace(/\./g, '_')
        expect(i18n.global.te(key, locale), `${locale} ${action}`).toBe(true)
      }
    }
  })
})
