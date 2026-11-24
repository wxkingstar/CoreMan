import { ref } from 'vue'

/** 顶栏可切换的显示时区；按 IANA 名计算，夏令时由 Intl 处理。 */
export const DISPLAY_ZONES = ['America/New_York', 'America/Los_Angeles', 'Asia/Shanghai', 'UTC'] as const
export type DisplayZone = (typeof DISPLAY_ZONES)[number]
/** 业务时间以美东为准：没选过的浏览器默认美东。 */
const DEFAULT_ZONE: DisplayZone = 'America/New_York'

const STORAGE_KEY = 'coreman.displayZone'

function saved(): DisplayZone {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return (DISPLAY_ZONES as readonly string[]).includes(v ?? '') ? (v as DisplayZone) : DEFAULT_ZONE
  } catch {
    return DEFAULT_ZONE
  }
}

/** 当前显示时区（每个浏览器各自记住）；模板里读它的函数会随切换重新渲染。 */
export const displayZone = ref<DisplayZone>(saved())

export function setDisplayZone(zone: DisplayZone): void {
  displayZone.value = zone
  try { localStorage.setItem(STORAGE_KEY, zone) } catch { /* unavailable storage */ }
}
