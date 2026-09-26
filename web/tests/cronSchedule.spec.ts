import { describe, expect, it } from 'vitest'
import { defaultSpec, parseCron, toCron } from '@/components/cron/schedule'

describe('cron schedule presets', () => {
  it.each([
    ['0 9 * * *', 'daily'], ['30 8 * * 1-5', 'weekdays'], ['0 9 * * 1,2,3,4,5', 'weekdays'],
    ['0 9 * * 1,3', 'weekly'], ['0 9 * * 0-6', 'daily'], ['0 18 1,15 * *', 'monthly'],
    ['5 * * * *', 'hourly'], ['0 */2 * * *', 'hourly'], ['*/15 * * * *', 'minutes'], ['* * * * *', 'minutes'],
  ])('recognizes %s as %s', (expression, preset) => {
    expect(parseCron(expression)?.preset).toBe(preset)
  })

  it.each(['0 9 * 1 *', '0 9 1 * 1', '0 9 L * *', '0 9 * * MON', '0 8-18 * * *', '0 9 * *', '61 9 * * *'])('leaves %s to custom editing', (expression) => {
    expect(parseCron(expression)).toBeNull()
  })

  it('round-trips every generated expression', () => {
    const base = defaultSpec()
    const specs = [
      { ...base, preset: 'daily' as const, hour: 7, minute: 45 },
      { ...base, preset: 'weekdays' as const },
      { ...base, preset: 'weekly' as const, weekdays: [6, 0, 3] },
      { ...base, preset: 'monthly' as const, monthDays: [15, 1, 31] },
      { ...base, preset: 'hourly' as const, hours: 4, minute: 10 },
      { ...base, preset: 'minutes' as const, interval: 20 },
    ]
    expect(specs.map(toCron)).toEqual(['45 7 * * *', '0 9 * * 1-5', '0 9 * * 0,3,6', '0 9 1,15,31 * *', '10 */4 * * *', '*/20 * * * *'])
    for (const spec of specs) expect(parseCron(toCron(spec))?.preset).toBe(spec.preset)
  })

  it('maps weekday 7 to Sunday', () => {
    expect(parseCron('0 9 * * 5-7')?.weekdays).toEqual([0, 5, 6])
  })
})
