import { describe, expect, it } from 'vitest'
import { EnvTextError, parseEnv, serializeEnv } from '@/utils/dotenv'

describe('skill dotenv editor', () => {
  it('accepts pasted dotenv syntax without evaluating values', () => {
    expect(parseEnv('\uFEFF# credentials\r\nexport API_KEY="a#b=c" # comment\r\nURL=https://example.com?a=b\nEMPTY=\nLITERAL=\'${TOKEN} $(whoami)\'\nTEXT="first\nsecond"')).toEqual([
      { key: 'API_KEY', value: 'a#b=c' }, { key: 'URL', value: 'https://example.com?a=b' },
      { key: 'EMPTY', value: '' }, { key: 'LITERAL', value: '${TOKEN} $(whoami)' },
      { key: 'TEXT', value: 'first\nsecond' },
    ])
  })
  it('round trips masks, whitespace, quotes, hashes, newlines and backslashes', () => {
    const entries = ['********abcdef', ' leading and trailing ', 'a"b#c=d', 'C:\\new\\test', 'first\nsecond\r\tend', '${TOKEN}', "it's fine", ''].map((value, i) => ({ key: `KEY_${i}`, value }))
    expect(parseEnv(serializeEnv(entries))).toEqual(entries)
  })
  it.each([
    ['A=1\n# comment\nA=2', 'duplicate', 3],
    ['A=1\nBROKEN', 'syntax', 2],
    ['A="unfinished', 'quote', 1],
    ['A="value" trailing', 'syntax', 1],
  ])('reports location without echoing secret text', (input, kind, line) => {
    try { parseEnv(input as string); expect.unreachable() }
    catch (e) { expect(e).toBeInstanceOf(EnvTextError); expect(e).toMatchObject({ kind, line }); expect(String(e)).not.toContain(input) }
  })
})
