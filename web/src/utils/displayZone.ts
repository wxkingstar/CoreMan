import { ref } from 'vue'

/** 顶栏可切换的显示时区；按 IANA 名计算，夏令时由 Intl 处理。 */
export const DISPLAY_ZONES = ['Asia/Shanghai', 'America/New_York', 'America/Los_Angeles'] as const
export type DisplayZone = (typeof DISPLAY_ZONES)[number]

const STORAGE_KEY = 'coreman.displayZone'

function isDisplayZone(v: string | null | undefined): v is DisplayZone {
  return (DISPLAY_ZONES as readonly string[]).includes(v ?? '')
}

/** 某时区此刻相对 UTC 的分钟数。 */
function offsetMinutes(timeZone: string, at: Date): number {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', second: 'numeric',
    }).formatToParts(at).map((x) => [x.type, Number(x.value)]),
  )
  const local = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second)
  return Math.round((local - Math.floor(at.getTime() / 1000) * 1000) / 60000)
}

/** 没选过时按本机系统时区取默认：本身就是三者之一直接用，否则取此刻偏移相同的，都不同则北京时间。 */
export function systemDisplayZone(systemZone?: string, at: Date = new Date()): DisplayZone {
  let zone = systemZone
  try { zone ??= Intl.DateTimeFormat().resolvedOptions().timeZone } catch { /* no Intl zone */ }
  if (isDisplayZone(zone)) return zone
  if (!zone) return 'Asia/Shanghai'
  try {
    const offset = offsetMinutes(zone, at)
    return DISPLAY_ZONES.find((z) => offsetMinutes(z, at) === offset) ?? 'Asia/Shanghai'
  } catch {
    return 'Asia/Shanghai'
  }
}

function saved(): DisplayZone {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (isDisplayZone(v)) return v
  } catch { /* unavailable storage */ }
  return systemDisplayZone()
}

/** 当前显示时区（每个浏览器各自记住所选）；模板里读它的函数会随切换重新渲染。 */
export const displayZone = ref<DisplayZone>(saved())

export function setDisplayZone(zone: DisplayZone): void {
  displayZone.value = zone
  try { localStorage.setItem(STORAGE_KEY, zone) } catch { /* unavailable storage */ }
}
