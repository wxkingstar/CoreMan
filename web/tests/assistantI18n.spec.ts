import { describe, expect, it } from 'vitest'
import { i18n } from '@/i18n'
import { assistantEn, assistantJa, assistantZh } from '@/i18n/assistant'

function flatten(value: object, prefix = ''): Record<string, string> {
  return Object.fromEntries(Object.entries(value).flatMap(([key, entry]) => {
    const path = prefix ? `${prefix}.${key}` : key
    return typeof entry === 'string' ? [[path, entry]] : Object.entries(flatten(entry, path))
  }))
}
const placeholders = (text: string) => text.match(/\{\w+\}/g)?.sort() ?? []
// tests/i18n.spec.ts 的渲染用例只提供这些插值参数：用了别的名字，那条用例会因为缺参数而告警。
const ALLOWED = ['{name}', '{count}', '{n}', '{total}', '{time}', '{value}', '{detail}', '{field}', '{start}', '{end}']

describe('assistant messages', () => {
  const zh = flatten(assistantZh), en = flatten(assistantEn), ja = flatten(assistantJa)

  it('has the same keys and placeholders in all three languages', () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort())
    expect(Object.keys(ja).sort()).toEqual(Object.keys(zh).sort())
    for (const [key, source] of Object.entries(zh)) {
      expect(placeholders(en[key]), key).toEqual(placeholders(source))
      expect(placeholders(ja[key]), key).toEqual(placeholders(source))
      for (const name of placeholders(source)) expect(ALLOWED, key).toContain(name)
    }
  })

  it('leaves no message empty and avoids vue-i18n syntax characters', () => {
    for (const messages of [zh, en, ja]) {
      for (const [key, text] of Object.entries(messages)) {
        expect(text.trim(), key).not.toBe('')
        expect(text.replace(/\{\w+\}/g, ''), key).not.toMatch(/[@{}|]/)
      }
    }
  })

  it('keeps Chinese and Japanese characters out of the English messages', () => {
    for (const [key, text] of Object.entries(en)) {
      expect(text, key).not.toMatch(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u)
    }
  })

  it('is mounted under assistant with menu names in every locale', () => {
    for (const locale of ['zh', 'en', 'ja'] as const) {
      const messages = i18n.global.messages.value[locale] as unknown as Record<string, Record<string, unknown>>
      expect(messages.assistant.page, locale).toBeTruthy()
      expect(messages.menu.myAssistant, locale).toBeTruthy()
      expect(messages.menu.assistants, locale).toBeTruthy()
    }
    expect(i18n.global.t('assistant.followups.categories.decide')).toBe('等我决定')
  })
})
