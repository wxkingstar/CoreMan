import type { AssistantPrefs, AssistantPrefsPatch } from '@/api/myAssistant'

/** 「我的助理」设置表单的值：两个会话列表在表单里是多行文本，一行一个 chat_id。 */
export interface PrefsForm {
  timezone: string
  work_start: string
  work_end: string
  workdays: number[]
  focus_chats: string
  ignored_chats: string
  /** 跟随平台默认：提交时每日预算传 null。 */
  follow_platform: boolean
  daily_turn_budget: number
}
export type PrefsField = 'timezone' | 'work_start' | 'work_end' | 'workdays' | 'focus_chats' | 'ignored_chats' | 'daily_turn_budget'

const CLOCK = /^([01][0-9]|2[0-3]):[0-5][0-9]$/
const CHAT = /^oc_[A-Za-z0-9_-]{1,80}$/

/** 多行文本 → 去掉空行与重复之后的各行。 */
export function lines(text: string): string[] {
  return [...new Set(text.split('\n').map((line) => line.trim()).filter(Boolean))]
}

export function toForm(prefs: AssistantPrefs): PrefsForm {
  return {
    timezone: prefs.timezone,
    work_start: prefs.work_start,
    work_end: prefs.work_end,
    workdays: [...prefs.workdays],
    focus_chats: prefs.focus_chats.join('\n'),
    ignored_chats: prefs.ignored_chats.join('\n'),
    follow_platform: prefs.daily_turn_budget === null,
    daily_turn_budget: prefs.daily_turn_budget ?? prefs.platform_daily_turn_budget,
  }
}

/** 浏览器认不认识这个 IANA 时区名。 */
export function validTimezone(name: string): boolean {
  if (!name.trim()) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: name.trim() })
    return true
  } catch {
    return false
  }
}

/**
 * 出错的字段 → 文案键（`assistant.settings.errors` 之下）；没有错误是空对象。
 * 与后端 `coreman/core/assistants/settings.py` 的 `update` 同一套规则，为的是不发一个注定被拒的请求；后端仍是最终裁决。
 */
export function validate(form: PrefsForm, prefs: AssistantPrefs): Partial<Record<PrefsField, string>> {
  const errors: Partial<Record<PrefsField, string>> = {}
  if (!validTimezone(form.timezone)) errors.timezone = 'timezone'
  if (!CLOCK.test(form.work_start)) errors.work_start = 'time'
  if (!CLOCK.test(form.work_end)) errors.work_end = 'time'
  // HH:MM 定宽，按字典序比就是按时间比。
  if (!errors.work_start && !errors.work_end && form.work_start >= form.work_end) errors.work_end = 'hours'
  if (!form.workdays.length) errors.workdays = 'workdays'
  const focus = lines(form.focus_chats), ignored = lines(form.ignored_chats)
  if (focus.some((id) => !CHAT.test(id))) errors.focus_chats = 'chatId'
  else if (focus.length > prefs.limits.focus_chats) errors.focus_chats = 'focusTooMany'
  if (ignored.some((id) => !CHAT.test(id))) errors.ignored_chats = 'chatId'
  else if (ignored.length > prefs.limits.ignored_chats) errors.ignored_chats = 'ignoredTooMany'
  else if (!errors.focus_chats && ignored.some((id) => focus.includes(id))) errors.ignored_chats = 'chatInBoth'
  const budget = form.daily_turn_budget
  // 没动过的个人值不校验：管理员把平台默认调到它以下之后，它高于上限但只是不再生效（生效的是两者里
  // 较小的），不该挡住所有者改别的项。
  const untouched = !form.follow_platform && budget === prefs.daily_turn_budget
  if (!form.follow_platform && !untouched && (!Number.isInteger(budget) || budget < prefs.limits.budget_min || budget > prefs.platform_daily_turn_budget)) {
    errors.daily_turn_budget = 'budget'
  }
  return errors
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/** 表单 → 请求体，只带改过的键。 */
export function toPatch(form: PrefsForm, prefs: AssistantPrefs): AssistantPrefsPatch {
  const next = {
    timezone: form.timezone.trim(),
    work_start: form.work_start,
    work_end: form.work_end,
    workdays: [...form.workdays].sort((a, b) => a - b),
    focus_chats: lines(form.focus_chats),
    ignored_chats: lines(form.ignored_chats),
    daily_turn_budget: form.follow_platform ? null : form.daily_turn_budget,
  }
  const patch: Record<string, unknown> = {}
  for (const key of Object.keys(next) as (keyof typeof next)[]) {
    if (!same(next[key], prefs[key])) patch[key] = next[key]
  }
  return patch as AssistantPrefsPatch
}
