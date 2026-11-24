import { ref } from 'vue'

/** 顶栏可切换的显示时区；按 IANA 名计算，夏令时由 Intl 处理。 */
export const DISPLAY_ZONES = ['Asia/Shanghai', 'America/New_York', 'UTC'] as const
export type DisplayZone = (typeof DISPLAY_ZONES)[number]

const STORAGE_KEY = 'coreman.displayZone'

function saved(): DisplayZone {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return (DISPLAY_ZONES as readonly string[]).includes(v ?? '') ? (v as DisplayZone) : 'Asia/Shanghai'
  } catch {
    return 'Asia/Shanghai'
  }
}

/** 当前显示时区（每个浏览器各自记住）；模板里读它的函数会随切换重新渲染。 */
export const displayZone = ref<DisplayZone>(saved())

export function setDisplayZone(zone: DisplayZone): void {
  displayZone.value = zone
  try { localStorage.setItem(STORAGE_KEY, zone) } catch { /* unavailable storage */ }
}
