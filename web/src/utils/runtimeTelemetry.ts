import type { HealthStatus, QuotaObservation, RelayOut, RuntimeHealthObservation } from '@/api/types'
import { pctNum } from '@/utils/format'

export function observedRecently(value: string | null | undefined, now: number, seconds: number) {
  const at = Date.parse(value ?? '')
  return Number.isFinite(at) && at <= now + 30_000 && now - at <= seconds * 1000
}

export function modelHealth(value: RuntimeHealthObservation | null | undefined, now = Date.now()): HealthStatus {
  if (!value || !observedRecently(value.observed_at, now, 900) || !['model', 'conversation'].includes(value.check_kind)) return 'unknown'
  return value.status === 'healthy' && value.reply_verified !== true ? 'unknown' : value.status
}

export type QuotaSource = Partial<Pick<RelayOut, 'rate_limit_5h_used_pct' | 'rate_limit_7d_used_pct' | 'rate_limit_5h_resets_at' | 'rate_limit_7d_resets_at' | 'rate_limit_probed_at'>> & {quota_observation?: QuotaObservation | null}
export function quotaWindows(source: QuotaSource, provider: string, now = Date.now()) {
  const metadata = source.quota_observation
  return (['five_hour', 'seven_day'] as const).map((key, index) => {
    const window = metadata?.windows?.[key]
    const pct = pctNum((window ? window.used_percentage : (index ? source.rate_limit_7d_used_pct : source.rate_limit_5h_used_pct)) ?? null)
    const resetsAt = (window ? window.resets_at : (index ? source.rate_limit_7d_resets_at : source.rate_limit_5h_resets_at)) ?? null
    const observedAt = (window ? window.observed_at : source.rate_limit_probed_at) ?? null
    let state = 'not_provided'
    if (window?.applicability === 'not_applicable') state = 'not_applicable'
    else if (metadata?.read_status === 'unsupported') state = 'unsupported'
    else if (pct !== null) state = metadata?.read_status === 'observed' && window?.status === 'observed' && observedRecently(observedAt, now, provider === 'claude' ? 1200 : 3900) && (!resetsAt || Date.parse(resetsAt) > now) ? 'fresh' : 'history'
    else if (metadata?.read_status === 'error') state = 'error'
    return {label: index ? '7d' : '5h', pct, resetsAt, observedAt, state}
  })
}
