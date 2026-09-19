import type { ChatLogOut } from '@/api/types'
import { formatBytes } from '@/utils/format'

// 状态 tag 配色：进行中主色、成功绿、出错与失败红、超时与中止橙、等待补充灰蓝。
type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

const STATUS_TYPE: Record<string, TagType> = {
  running: 'primary',
  success: 'success',
  error: 'danger',
  failed: 'danger',
  timeout: 'warning',
  stopped: 'warning',
  ask_user: 'info',
}

/** 后端列表项没有 platform_user_id：姓名、登录名都为空（未映射到内部用户）时退到 user_id。 */
export function userOf(row: ChatLogOut): string {
  return row.user_name || row.user_login || row.user_id || '—'
}

export function statusType(status: string): TagType {
  return STATUS_TYPE[status] ?? 'info'
}

/** `file_info` 是后端透传的 JSON（企微文件消息的 filename / size / mime），字段缺失时逐个兜底。 */
export function fileLine(info: Record<string, unknown>): string {
  if (Array.isArray(info.files)) return info.files.filter((f) => f && typeof f === 'object').map((f) => fileLine(f as Record<string, unknown>)).join('；')
  const { filename, mime, size } = info as { filename?: unknown; mime?: unknown; size?: unknown }
  return `${filename ? String(filename) : '—'} · ${mime ? String(mime) : '—'} · ${formatBytes(Number(size ?? Number.NaN))}`
}

function num(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : String(v)
}

export function tokensOf(row: ChatLogOut): string {
  return `${num(row.input_tokens)} / ${num(row.output_tokens)}`
}

export function latencyOf(ms: number | null): string {
  return ms === null ? '—' : `${ms} ms`
}
