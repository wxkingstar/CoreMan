import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/myAssistant', async (original) => ({
  ...(await original<typeof import('@/api/myAssistant')>()),
  myAssistant: { settings: vi.fn(), saveSettings: vi.fn(), deprovision: vi.fn() },
}))

import { myAssistant, type AssistantPrefs } from '@/api/myAssistant'
import AssistantSettings from '@/components/assistant/AssistantSettings.vue'
import { i18n } from '@/i18n'
import { lines, toForm, toPatch, validate, validTimezone, type PrefsField, type PrefsForm } from '@/utils/assistantSettings'

const t = i18n.global.t
const prefs: AssistantPrefs = {
  timezone: 'Asia/Shanghai', work_start: '09:30', work_end: '19:00', workdays: [1, 2, 3, 4, 5],
  focus_chats: ['oc_a'], ignored_chats: [], daily_turn_budget: null, platform_daily_turn_budget: 200, effective_daily_turn_budget: 200,
  limits: { focus_chats: 5, ignored_chats: 50, budget_min: 10 }, locked: false,
  on_shift: true, shift_override: null, shift_override_until: null, cold_start: 'done', today: '2026-11-13',
}
const CASES: [Partial<PrefsForm>, Partial<Record<PrefsField, string>>][] = [
  [{}, {}],
  [{ timezone: 'America/New_York' }, {}],
  [{ timezone: 'Asia/Nowhere' }, { timezone: 'timezone' }],
  [{ timezone: '  ' }, { timezone: 'timezone' }],
  [{ work_start: '9:30' }, { work_start: 'time' }],
  [{ work_end: '25:00' }, { work_end: 'time' }],
  [{ work_start: '20:00' }, { work_end: 'hours' }],
  [{ work_start: '19:00' }, { work_end: 'hours' }],
  [{ workdays: [] }, { workdays: 'workdays' }],
  [{ focus_chats: 'oc_a\nnot-a-chat' }, { focus_chats: 'chatId' }],
  [{ focus_chats: 'oc_1\noc_2\noc_3\noc_4\noc_5\noc_6' }, { focus_chats: 'focusTooMany' }],
  [{ ignored_chats: 'ou_person' }, { ignored_chats: 'chatId' }],
  [{ ignored_chats: 'oc_b\noc_a' }, { ignored_chats: 'chatInBoth' }],
  [{ follow_platform: false, daily_turn_budget: 9 }, { daily_turn_budget: 'budget' }],
  [{ follow_platform: false, daily_turn_budget: 201 }, { daily_turn_budget: 'budget' }],
  [{ follow_platform: false, daily_turn_budget: 20.5 }, { daily_turn_budget: 'budget' }],
  [{ follow_platform: false, daily_turn_budget: 200 }, {}],
  // 跟随平台默认时，输入框里留着什么数都不校验。
  [{ follow_platform: true, daily_turn_budget: 9999 }, {}],
]

describe('settings form rules', () => {
  it.each(CASES)('validate %j gives %j', (change, expected) => {
    expect(validate({ ...toForm(prefs), ...change }, prefs)).toEqual(expected)
  })

  it('knows time zones and splits chat lists', () => {
    expect(validTimezone('UTC')).toBe(true)
    expect(validTimezone('Mars/Olympus')).toBe(false)
    expect(lines(' oc_a \n\noc_b\noc_a\n')).toEqual(['oc_a', 'oc_b'])
    expect(toForm({ ...prefs, daily_turn_budget: 80 })).toMatchObject({ follow_platform: false, daily_turn_budget: 80, focus_chats: 'oc_a' })
    expect(toForm(prefs)).toMatchObject({ follow_platform: true, daily_turn_budget: 200 })
  })

  it('lets a personal budget above a lowered platform default stand, but not be raised', () => {
    // 管理员把平台默认调到了个人值以下：个人值留着（生效的是较小的），不挡住改别的项。
    const lowered = { ...prefs, daily_turn_budget: 150, platform_daily_turn_budget: 80, effective_daily_turn_budget: 80 }
    expect(validate(toForm(lowered), lowered)).toEqual({})
    expect(validate({ ...toForm(lowered), timezone: 'UTC' }, lowered)).toEqual({})
    expect(validate({ ...toForm(lowered), daily_turn_budget: 160 }, lowered)).toEqual({ daily_turn_budget: 'budget' })
    expect(validate({ ...toForm(lowered), daily_turn_budget: 81 }, lowered)).toEqual({ daily_turn_budget: 'budget' })
    expect(validate({ ...toForm(lowered), daily_turn_budget: 60 }, lowered)).toEqual({})
    expect(toPatch({ ...toForm(lowered), timezone: 'UTC' }, lowered)).toEqual({ timezone: 'UTC' })
  })

  it('builds a patch with only what changed', () => {
    expect(toPatch(toForm(prefs), prefs)).toEqual({})
    expect(toPatch({ ...toForm(prefs), timezone: 'UTC', workdays: [5, 1], focus_chats: 'oc_a\noc_b' }, prefs)).toEqual({
      timezone: 'UTC', workdays: [1, 5], focus_chats: ['oc_a', 'oc_b'],
    })
    expect(toPatch({ ...toForm(prefs), follow_platform: false, daily_turn_budget: 120 }, prefs)).toEqual({ daily_turn_budget: 120 })
    const own = { ...prefs, daily_turn_budget: 120 }
    expect(toPatch({ ...toForm(own), follow_platform: true }, own)).toEqual({ daily_turn_budget: null })
  })
})

const render = () => mount(AssistantSettings, {
  props: { name: '个人助理', runtime: 'pool-1' },
  global: { plugins: [ElementPlus, i18n], stubs: { AssistantAccessLog: true } },
})
const form = (wrapper: ReturnType<typeof render>) => (wrapper.vm as unknown as { form: PrefsForm }).form

describe('settings section', () => {
  it('shows the access log under the form', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('assistant-access-log-stub').exists()).toBe(true)
    wrapper.unmount()
  })

  beforeEach(() => {
    vi.restoreAllMocks()
    vi.mocked(myAssistant.settings).mockReset().mockResolvedValue(prefs)
    vi.mocked(myAssistant.saveSettings).mockReset().mockImplementation(async (patch) => ({ ...prefs, ...patch }) as AssistantPrefs)
    vi.mocked(myAssistant.deprovision).mockReset().mockResolvedValue({ state: 'deprovisioning' })
    vi.spyOn(ElMessage, 'success').mockReturnValue({ close: () => {} })
  })

  it('loads the current values and shows the runtime', async () => {
    const wrapper = render(); await flushPromises()
    expect(form(wrapper)).toMatchObject({ timezone: 'Asia/Shanghai', work_start: '09:30', follow_platform: true })
    expect(wrapper.get('[data-test="settings-runtime"]').text()).toContain('pool-1')
    expect(wrapper.text()).toContain(t('assistant.settings.budgetFollow', { value: 200 }))
    wrapper.unmount()
  })

  it('blocks saving and shows the reason when a rule is broken', async () => {
    const wrapper = render(); await flushPromises()
    form(wrapper).work_start = '20:00'
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(myAssistant.saveSettings).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain(t('assistant.settings.errors.hours'))
    form(wrapper).work_start = '08:00'
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(myAssistant.saveSettings).toHaveBeenCalledWith({ work_start: '08:00' })
    expect(wrapper.text()).not.toContain(t('assistant.settings.errors.hours'))
    wrapper.unmount()
  })

  it('sends only the changed keys', async () => {
    const wrapper = render(); await flushPromises()
    Object.assign(form(wrapper), { timezone: 'UTC', follow_platform: false, daily_turn_budget: 120, ignored_chats: 'oc_noise' })
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(myAssistant.saveSettings).toHaveBeenCalledWith({ timezone: 'UTC', ignored_chats: ['oc_noise'], daily_turn_budget: 120 })
    wrapper.unmount()
  })

  it('says which budget applies when the platform default dropped below the personal one', async () => {
    const wrapper = render(); await flushPromises()
    expect(wrapper.find('[data-test="budget-effective"]').exists()).toBe(false)
    wrapper.unmount()
    vi.mocked(myAssistant.settings).mockResolvedValue({ ...prefs, daily_turn_budget: 150, platform_daily_turn_budget: 80, effective_daily_turn_budget: 80 })
    const lowered = render(); await flushPromises()
    expect(lowered.get('[data-test="budget-effective"]').text()).toBe(t('assistant.settings.budgetEffective', { value: 80 }))
    lowered.unmount()
  })

  it('does not call the API when nothing changed', async () => {
    const info = vi.spyOn(ElMessage, 'info').mockReturnValue({ close: () => {} })
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(myAssistant.saveSettings).not.toHaveBeenCalled()
    expect(info).toHaveBeenCalledWith(t('assistant.settings.nothingChanged'))
    wrapper.unmount()
  })

  it('shows the backend message when the change is refused', async () => {
    const error = vi.spyOn(ElMessage, 'error').mockReturnValue({ close: () => {} })
    vi.mocked(myAssistant.saveSettings).mockRejectedValueOnce(new Error('每日轮次预算不能超过平台默认值'))
    const wrapper = render(); await flushPromises()
    form(wrapper).timezone = 'UTC'
    await wrapper.get('[data-test="save-settings"]').trigger('click'); await flushPromises()
    expect(error).toHaveBeenCalledWith('每日轮次预算不能超过平台默认值')
    wrapper.unmount()
  })

  it('shuts the assistant down only after its name is typed', async () => {
    const prompt = vi.spyOn(ElMessageBox, 'prompt').mockResolvedValue({ value: '个人助理', action: 'confirm' } as never)
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="deprovision"]').trigger('click'); await flushPromises()
    expect(prompt.mock.calls[0][0]).toBe(t('assistant.settings.deprovisionPrompt', { name: '个人助理' }))
    // 停用前提示可以先导出。
    expect(prompt.mock.calls[0][0]).toContain('导出')
    const options = prompt.mock.calls[0][2] as { inputValidator: (value: string) => boolean | string }
    expect(options.inputValidator('个人助理')).toBe(true)
    expect(options.inputValidator('个人')).toBe(t('assistant.settings.deprovisionMismatch'))
    expect(options.inputValidator('')).toBe(t('assistant.settings.deprovisionMismatch'))
    expect(myAssistant.deprovision).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('deprovisioned')).toHaveLength(1)
    wrapper.unmount()
  })

  it('does not shut down when the confirmation is dismissed', async () => {
    vi.spyOn(ElMessageBox, 'prompt').mockRejectedValue('cancel')
    const wrapper = render(); await flushPromises()
    await wrapper.get('[data-test="deprovision"]').trigger('click'); await flushPromises()
    expect(myAssistant.deprovision).not.toHaveBeenCalled()
    expect(wrapper.emitted('deprovisioned')).toBeUndefined()
    wrapper.unmount()
  })
})
