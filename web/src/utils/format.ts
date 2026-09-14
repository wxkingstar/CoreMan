import type { Pct } from '@/api/types'

// hourCycle 显式给 h23：zh-CN 的默认时制是 12 小时，只写 hour12: false 在部分 ICU 上
// 会落到 h24，午夜渲染成 24:00:00 而不是 00:00:00。
const fmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  hourCycle: 'h23',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

/** 东八区 `YYYY-MM-DD HH:mm:ss`；空值或非法时间返回 '—'（spec §10.3：时间东八区）。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  const p = Object.fromEntries(fmt.formatToParts(d).map((x) => [x.type, x.value]))
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`
}

const BYTE_UNITS = ['KB', 'MB', 'GB', 'TB'] as const

/**
 * 文件体积：不足 1 KB 按整字节显示，其余进位到 KB/MB/GB/TB 保留一位小数（1024 → `1.0 KB`）。
 * 来源是 `chat_logs.file_info` 里后端透传的 JSON，字段缺失时上层会传进 NaN，这里统一回 '—'。
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(1)} ${BYTE_UNITS[unit]}`
}

/** Numeric 列可能序列化成字符串，比较与渲染之前统一过一次 Number()。 */
export function pctNum(value: Pct): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  return Number.isNaN(n) ? null : n
}

/** 限额进度条配色：>90% 红、60–90% 橙、其余绿。 */
export function pctColor(value: Pct): string {
  const n = pctNum(value) ?? 0
  if (n > 90) return '#f56c6c'
  if (n >= 60) return '#e6a23c'
  return '#67c23a'
}
