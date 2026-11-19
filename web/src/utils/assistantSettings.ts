import type { AssistantPrefs, AssistantPrefsPatch } from '@/api/myAssistant'

/** 「我的助理」设置表单的值：重点群与托管名单都由会话选择器给出 chat_id 列表。 */
export interface PrefsForm {
  timezone: string
  /** 全天候：开着时工作时段与工作日不生效，表单里保留原值。 */
  always_on: boolean
  work_start: string
  work_end: string
  workdays: number[]
  focus_chats: string[]
  /** 托管范围：all 托管全部（ignored_chats 除外），listed 只托管 managed_chats。 */
  chat_scope: 'all' | 'listed'
  ignored_chats: string[]
  managed_chats: string[]
  /** 摘要时点：表单里是一行文本，逗号或空格隔开。 */
  digest_times: string
  quiet_enabled: boolean
  quiet_start: string
  quiet_end: string
  /** 跟随平台默认：提交时每日预算传 null。 */
  follow_platform: boolean
  daily_turn_budget: number
}
export type PrefsField = 'timezone' | 'work_start' | 'work_end' | 'workdays' | 'focus_chats' | 'ignored_chats' | 'managed_chats' | 'digest_times' | 'quiet_hours' | 'daily_turn_budget'

const CLOCK = /^([01][0-9]|2[0-3]):[0-5][0-9]$/
/** 与后端 `settings.MAX_DIGEST_TIMES` 一致。 */
export const MAX_DIGEST_TIMES = 6
const CHAT = /^oc_[A-Za-z0-9_-]{1,80}$/

export function toForm(prefs: AssistantPrefs): PrefsForm {
  return {
    timezone: prefs.timezone,
    always_on: prefs.always_on,
    work_start: prefs.work_start,
    work_end: prefs.work_end,
    workdays: [...prefs.workdays],
    focus_chats: [...prefs.focus_chats],
    chat_scope: prefs.chat_scope,
    ignored_chats: [...prefs.ignored_chats],
    managed_chats: [...prefs.managed_chats],
    digest_times: prefs.digest_times.join(', '),
    quiet_enabled: prefs.quiet_enabled,
    quiet_start: prefs.quiet_start,
    quiet_end: prefs.quiet_end,
    follow_platform: prefs.daily_turn_budget === null,
    daily_turn_budget: prefs.daily_turn_budget ?? prefs.platform_daily_turn_budget,
  }
}

/** 摘要时点的一行文本 → 各个时点（按逗号、顿号或空白分开），去空、升序。重复的留着，交给校验报错。 */
export function clocks(text: string): string[] {
  return text.split(/[\s,，、]+/).map((item) => item.trim()).filter(Boolean).sort()
}

/** 时区只给这几个选：所有者在这三个地方办公。已存的别的时区仍原样显示、可保留。 */
export const TIMEZONES = ['Asia/Singapore', 'America/New_York', 'America/Los_Angeles'] as const

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
  // 全天候时工作时段与工作日置灰、改不了，不校验：存着的值本来就合法。
  if (!form.always_on) {
    if (!CLOCK.test(form.work_start)) errors.work_start = 'time'
    if (!CLOCK.test(form.work_end)) errors.work_end = 'time'
    // HH:MM 定宽，按字典序比就是按时间比。
    if (!errors.work_start && !errors.work_end && form.work_start >= form.work_end) errors.work_end = 'hours'
    if (!form.workdays.length) errors.workdays = 'workdays'
  }
  const focus = [...new Set(form.focus_chats)], ignored = form.ignored_chats, managed = form.managed_chats
  if (focus.some((id) => !CHAT.test(id))) errors.focus_chats = 'chatId'
  else if (focus.length > prefs.limits.focus_chats) errors.focus_chats = 'focusTooMany'
  // 托管范围只校验当前生效的那份名单；另一份原样保留，切回去时还在。
  if (form.chat_scope === 'all') {
    if (ignored.length > prefs.limits.ignored_chats) errors.ignored_chats = 'ignoredTooMany'
    else if (!errors.focus_chats && ignored.some((id) => focus.includes(id))) errors.ignored_chats = 'chatInBoth'
  } else if (managed.length > prefs.limits.managed_chats) errors.managed_chats = 'managedTooMany'
  else if (!errors.focus_chats && focus.some((id) => !managed.includes(id))) errors.managed_chats = 'focusNotManaged'
  const times = clocks(form.digest_times)
  if (!times.length || times.length > MAX_DIGEST_TIMES || new Set(times).size !== times.length || times.some((time) => !CLOCK.test(time))) {
    errors.digest_times = 'digestTimes'
  }
  if (!CLOCK.test(form.quiet_start) || !CLOCK.test(form.quiet_end) || form.quiet_start === form.quiet_end) errors.quiet_hours = 'quietHours'
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
    always_on: form.always_on,
    work_start: form.work_start,
    work_end: form.work_end,
    workdays: [...form.workdays].sort((a, b) => a - b),
    focus_chats: [...new Set(form.focus_chats)],
    chat_scope: form.chat_scope,
    ignored_chats: [...new Set(form.ignored_chats)],
    managed_chats: [...new Set(form.managed_chats)],
    digest_times: clocks(form.digest_times),
    quiet_enabled: form.quiet_enabled,
    quiet_start: form.quiet_start,
    quiet_end: form.quiet_end,
    daily_turn_budget: form.follow_platform ? null : form.daily_turn_budget,
  }
  const patch: Record<string, unknown> = {}
  for (const key of Object.keys(next) as (keyof typeof next)[]) {
    if (!same(next[key], prefs[key])) patch[key] = next[key]
  }
  return patch as AssistantPrefsPatch
}
