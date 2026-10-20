import { describe, expect, it } from 'vitest'
import { formatBytes, formatDateTime, resetCountdown } from '@/utils/format'

describe('formatDateTime', () => {
  it('renders in Asia/Shanghai regardless of host timezone', () => {
    expect(formatDateTime('2026-09-10T00:30:00Z')).toBe('2026-09-10 08:30:00')
    expect(formatDateTime('2026-09-10T08:30:00+08:00')).toBe('2026-09-10 08:30:00')
  })
  it('renders Shanghai midnight as 00:00:00, not 24:00:00', () => {
    // hourCycle 若落到 h24，这里会渲染成 24:00:00。
    expect(formatDateTime('2026-09-09T16:00:00Z')).toBe('2026-09-10 00:00:00')
  })
  it('handles empty values', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime(undefined)).toBe('—')
    expect(formatDateTime('')).toBe('—')
  })
})

describe('formatBytes', () => {
  it('keeps bytes whole and larger units at one decimal', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB')
    expect(formatBytes(3.5 * 1024 * 1024 * 1024)).toBe('3.5 GB')
  })
  it('handles missing or nonsensical sizes', () => {
    // file_info.size 是后端透传的 JSON，缺字段时上层会传进 NaN。
    expect(formatBytes(Number.NaN)).toBe('—')
    expect(formatBytes(-1)).toBe('—')
  })
})

describe('resetCountdown', () => {
  const now = Date.parse('2026-10-21T00:00:00Z')
  it('splits the time left into days, hours and minutes', () => {
    expect(resetCountdown('2026-10-21T02:13:00Z', now)).toEqual({ d: 0, h: 2, m: 13 })
    expect(resetCountdown('2026-10-24T05:00:00Z', now)).toEqual({ d: 3, h: 5, m: 0 })
    // 不足一分钟按一分钟算，别显示成 0 分钟。
    expect(resetCountdown('2026-10-21T00:00:20Z', now)).toEqual({ d: 0, h: 0, m: 1 })
  })
  it('reports a reset time that has already passed', () => {
    expect(resetCountdown('2026-10-20T23:59:00Z', now)).toBe('done')
  })
  it('handles empty or invalid values', () => {
    expect(resetCountdown(null, now)).toBe(null)
    expect(resetCountdown('not a date', now)).toBe(null)
  })
})
