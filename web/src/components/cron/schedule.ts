/**
 * 触发时间的快捷选择与五字段 cron 之间的双向转换。
 * 只识别快捷选项能生成的写法；其余表达式归入 custom，原样保留给用户编辑。
 */
export type SchedulePreset = 'daily' | 'weekdays' | 'weekly' | 'monthly' | 'hourly' | 'minutes' | 'custom'
export const presets: SchedulePreset[] = ['daily', 'weekdays', 'weekly', 'monthly', 'hourly', 'minutes', 'custom']

export interface ScheduleSpec {
  preset: SchedulePreset
  hour: number
  minute: number
  /** cron 周字段取值，0 为周日。 */
  weekdays: number[]
  monthDays: number[]
  /** hourly：每几小时。 */
  hours: number
  /** minutes：每几分钟。 */
  interval: number
}

export function defaultSpec(): ScheduleSpec {
  return { preset: 'weekdays', hour: 9, minute: 0, weekdays: [1], monthDays: [1], hours: 1, interval: 15 }
}

const sorted = (values: number[]) => [...new Set(values)].sort((a, b) => a - b)

export function toCron(spec: ScheduleSpec): string {
  const { minute, hour } = spec
  switch (spec.preset) {
    case 'daily': return `${minute} ${hour} * * *`
    case 'weekdays': return `${minute} ${hour} * * 1-5`
    case 'weekly': return `${minute} ${hour} * * ${sorted(spec.weekdays).join(',') || '1'}`
    case 'monthly': return `${minute} ${hour} ${sorted(spec.monthDays).join(',') || '1'} * *`
    case 'hourly': return `${minute} ${spec.hours > 1 ? `*/${spec.hours}` : '*'} * * *`
    case 'minutes': return spec.interval > 1 ? `*/${spec.interval} * * * *` : '* * * * *'
    case 'custom': return ''
  }
}

function int(value: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(value)) return null
  const n = Number(value)
  return n >= min && n <= max ? n : null
}

/** 解析 `1,3-5` 这样的列表；任何一段不合法就返回 null。 */
function list(value: string, min: number, max: number): number[] | null {
  const out: number[] = []
  for (const part of value.split(',')) {
    const [a, b, ...rest] = part.split('-')
    const start = int(a ?? '', min, max)
    const end = b === undefined ? start : int(b, min, max)
    if (rest.length || start === null || end === null || end < start) return null
    for (let n = start; n <= end; n++) out.push(n)
  }
  return sorted(out)
}

/** 表达式能被快捷选项表达时返回对应设置，否则返回 null。 */
export function parseCron(expression: string): ScheduleSpec | null {
  const fields = expression.trim().split(/\s+/)
  if (fields.length !== 5) return null
  const [mi, h, dom, mon, dow] = fields as [string, string, string, string, string]
  const spec = defaultSpec()
  if (mon !== '*') return null
  if (h === '*' && dom === '*' && dow === '*') {
    if (mi === '*') return { ...spec, preset: 'minutes', interval: 1 }
    const every = /^\*\/(\d+)$/.exec(mi)
    const interval = every ? int(every[1]!, 1, 59) : null
    if (interval !== null) return { ...spec, preset: 'minutes', interval }
  }
  const minute = int(mi, 0, 59)
  if (minute === null) return null
  if (dom === '*' && dow === '*') {
    if (h === '*') return { ...spec, preset: 'hourly', minute, hours: 1 }
    const every = /^\*\/(\d+)$/.exec(h)
    const hours = every ? int(every[1]!, 1, 23) : null
    if (hours !== null) return { ...spec, preset: 'hourly', minute, hours }
  }
  const hour = int(h, 0, 23)
  if (hour === null) return null
  if (dom === '*' && dow === '*') return { ...spec, preset: 'daily', hour, minute }
  if (dom === '*') {
    const days = list(dow, 0, 7)
    if (!days) return null
    const weekdays = sorted(days.map(d => d % 7))
    if (weekdays.length === 7) return { ...spec, preset: 'daily', hour, minute }
    if (weekdays.join() === '1,2,3,4,5') return { ...spec, preset: 'weekdays', hour, minute, weekdays }
    return { ...spec, preset: 'weekly', hour, minute, weekdays }
  }
  if (dow === '*') {
    const monthDays = list(dom, 1, 31)
    return monthDays ? { ...spec, preset: 'monthly', hour, minute, monthDays } : null
  }
  return null
}

export const pad = (n: number) => String(n).padStart(2, '0')
