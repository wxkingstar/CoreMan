import { describe, expect, it } from 'vitest'
import { modelHealth, quotaWindows } from '../src/utils/runtimeTelemetry'
import type { RuntimeHealthObservation } from '../src/api/types'

const now = Date.parse('2026-11-04T07:00:00Z')
describe('runtime observations', () => {
  it('never promotes driver health or an unverified reply to model availability', () => {
    const observation: RuntimeHealthObservation = { status: 'healthy', check_kind: 'driver', observed_at: '2026-11-04T06:59:00Z', reply_verified: true }
    expect(modelHealth(observation, now)).toBe('unknown')
    expect(modelHealth({...observation, check_kind: 'model', reply_verified: false}, now)).toBe('unknown')
    expect(modelHealth({...observation, check_kind: 'conversation'}, now)).toBe('healthy')
    expect(modelHealth({...observation, check_kind: 'model', observed_at: '2026-11-04T06:00:00Z'}, now)).toBe('unknown')
  })
  it('distinguishes inapplicable windows, observed zero, stale and failed reads', () => {
    const source = { quota_observation: { read_status: 'observed', windows: {
      five_hour: {applicability:'not_applicable',status:'not_applicable'},
      seven_day: {used_percentage:0,status:'observed',observed_at:'2026-11-04T06:59:00Z',resets_at:'2026-11-11T00:00:00Z'},
    } } }
    expect(quotaWindows(source, 'codex', now).map(w=>w.state)).toEqual(['not_applicable','fresh'])
    expect(quotaWindows(source, 'codex', now)[1]?.pct).toBe(0)
    expect(quotaWindows(source, 'codex', now+4_000_000)[1]?.state).toBe('history')
    expect(quotaWindows({quota_observation:{read_status:'error'}},'claude',now)[0]?.state).toBe('error')
    expect(quotaWindows({quota_observation:{read_status:'unsupported'}},'opencode',now)[0]?.state).toBe('unsupported')
  })
  it('retains legacy numbers as history rather than repainting them as fresh',()=>{
    expect(quotaWindows({rate_limit_7d_used_pct:37,rate_limit_probed_at:'2026-11-04T06:59:00Z'},'codex',now)[1]).toMatchObject({pct:37,state:'history'})
  })
})
