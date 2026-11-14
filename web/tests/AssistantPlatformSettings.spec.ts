import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin', () => ({
  settings: { get: vi.fn(), update: vi.fn() },
  departments: { tree: vi.fn() },
  users: { get: vi.fn(), list: vi.fn() },
}))

import { departments, settings, users } from '@/api/admin'
import type { AssistantBackend } from '@/api/types'
import AssistantPlatformSettings from '@/components/AssistantPlatformSettings.vue'
import { i18n } from '@/i18n'

const t = i18n.global.t
const U1 = '11111111-1111-4111-8111-111111111111', D1 = '22222222-2222-4222-8222-222222222222'
// 键的顺序故意与表单里的不同：settings 表是 JSONB，读回来的键序不保证。
const server = {
  assistant_eligibility: { user_ids: [U1], mode: 'listed', department_ids: [] },
  assistant_backends: ['codex'],
  assistant_node_concurrency: 2,
  assistant_daily_turn_budget: 200,
}
interface Form {
  mode: 'all' | 'listed'; user_ids: string[]; department_ids: string[]
  primary: AssistantBackend; also_other: boolean; concurrency: number; budget: number
}
const render = () => mount(AssistantPlatformSettings, { global: { plugins: [ElementPlus, i18n] } })
const form = (wrapper: ReturnType<typeof render>) => (wrapper.vm as unknown as { form: Form }).form
const save = async (wrapper: ReturnType<typeof render>) => { await wrapper.get('[data-test="save-assistant-platform"]').trigger('click'); await flushPromises() }

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(settings.get).mockReset().mockResolvedValue({ ...server } as never)
  vi.mocked(settings.update).mockReset().mockImplementation(async (body) => ({ ...server, ...body }) as never)
  vi.mocked(departments.tree).mockReset().mockResolvedValue([{ id: D1, platform_dept_id: '1', name: '销售部', path: '/销售部', sort_order: 1, member_count: 3, children: [] }])
  vi.mocked(users.get).mockReset().mockResolvedValue({ id: U1, login_name: 'zhangsan', display_name: '张三' } as never)
  vi.mocked(users.list).mockReset().mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 } as never)
  vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
})

describe('assistant platform settings', () => {
  it('loads the four settings and the names of listed people', async () => {
    const wrapper = render(); await flushPromises()
    expect(form(wrapper)).toEqual({ mode: 'listed', user_ids: [U1], department_ids: [], primary: 'codex', also_other: false, concurrency: 2, budget: 200 })
    expect(departments.tree).toHaveBeenCalledWith('feishu')
    expect(users.get).toHaveBeenCalledWith(U1)
    expect(wrapper.text()).toContain(t('assistant.platform.title'))
    wrapper.unmount()
  })

  it('does not call the API when nothing changed, whatever the stored key order', async () => {
    const info = vi.spyOn(ElMessage, 'info').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await save(wrapper)
    expect(settings.update).not.toHaveBeenCalled()
    expect(info).toHaveBeenCalledWith(t('assistant.platform.nothingChanged'))
    wrapper.unmount()
  })

  it('submits only the changed setting: who may set one up', async () => {
    const wrapper = render(); await flushPromises()
    form(wrapper).department_ids = [D1]
    await save(wrapper)
    expect(settings.update).toHaveBeenCalledWith({ assistant_eligibility: { mode: 'listed', department_ids: [D1], user_ids: [U1] } })
    form(wrapper).mode = 'all'
    await save(wrapper)
    expect(settings.update).toHaveBeenLastCalledWith({ assistant_eligibility: { mode: 'all', department_ids: [D1], user_ids: [U1] } })
    wrapper.unmount()
  })

  it('submits the allowed AI in order of preference', async () => {
    const wrapper = render(); await flushPromises()
    form(wrapper).also_other = true
    await save(wrapper)
    expect(settings.update).toHaveBeenLastCalledWith({ assistant_backends: ['codex', 'claude'] })
    form(wrapper).primary = 'claude'
    await save(wrapper)
    expect(settings.update).toHaveBeenLastCalledWith({ assistant_backends: ['claude', 'codex'] })
    wrapper.unmount()
  })

  it('submits concurrency and the default budget', async () => {
    const wrapper = render(); await flushPromises()
    Object.assign(form(wrapper), { concurrency: 4, budget: 120 })
    await save(wrapper)
    expect(settings.update).toHaveBeenCalledWith({ assistant_node_concurrency: 4, assistant_daily_turn_budget: 120 })
    wrapper.unmount()
  })

  it('warns when the list is selected but empty', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="listed-empty"]').exists()).toBe(false)
    form(wrapper).user_ids = []
    await flushPromises()
    expect(wrapper.get('[data-test="listed-empty"]').text()).toContain(t('assistant.platform.listedEmpty'))
    wrapper.unmount()
  })

  it('falls back to defaults when the backend does not send the keys, and shows a refusal', async () => {
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(settings.get).mockResolvedValue({} as never)
    vi.mocked(settings.update).mockRejectedValueOnce(new Error('设置项不能置空'))
    const wrapper = render(); await flushPromises()
    expect(form(wrapper)).toEqual({ mode: 'all', user_ids: [], department_ids: [], primary: 'claude', also_other: true, concurrency: 2, budget: 2000 })
    form(wrapper).budget = 3000
    await save(wrapper)
    expect(error).toHaveBeenCalledWith('设置项不能置空')
    wrapper.unmount()
  })
})
