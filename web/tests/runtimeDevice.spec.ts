import { describe, expect, it } from 'vitest'
import { runtimeDevice } from '@/utils/runtimeDevice'

describe('runtimeDevice', () => {
  it('recognises Mac Studio / Mac mini regardless of case and separators', () => {
    for (const n of ['MacStudio-A / claude', 'mac-studio-b-assistant / claude', 'MAC_STUDIO', 'Mac Studio 2']) expect(runtimeDevice(n)).toBe('mac-studio')
    for (const n of ['MacMini-A / codex', 'mac-mini', 'Mac mini m4']) expect(runtimeDevice(n)).toBe('mac-mini')
  })
  it('falls back to server', () => {
    for (const n of ['node-a / codex', 'gpu-box', '', null, undefined]) expect(runtimeDevice(n)).toBe('server')
  })
})
