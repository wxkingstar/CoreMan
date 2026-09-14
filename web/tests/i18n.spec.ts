import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { ElConfigProvider } from 'element-plus'
import App from '@/App.vue'
import { getLocale, i18n, setLocale } from '@/i18n'

function flatten(value: object, prefix = ''): Record<string, string> {
  return Object.fromEntries(Object.entries(value).flatMap(([key, entry]) => {
    const path = prefix ? `${prefix}.${key}` : key
    return typeof entry === 'string' ? [[path, entry]] : Object.entries(flatten(entry, path))
  }))
}

beforeEach(() => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  })
})
afterEach(() => { vi.restoreAllMocks(); setLocale('zh'); vi.unstubAllGlobals() })

describe('English locale', () => {
  it('covers every Chinese message and preserves interpolation parameters', () => {
    const zh = flatten(i18n.global.messages.value.zh)
    const en = flatten(i18n.global.messages.value.en)
    expect(Object.keys(en)).toEqual(expect.arrayContaining(Object.keys(zh)))
    for (const [key, source] of Object.entries(zh)) {
      expect(en[key], key).toBeTruthy()
      expect(en[key], key).not.toMatch(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u)
      expect(en[key].match(/\{\w+\}/g)?.sort() ?? [], key).toEqual(source.match(/\{\w+\}/g)?.sort() ?? [])
    }
  })

  it('renders all English messages without falling back or parser errors', () => {
    setLocale('en')
    const error = vi.spyOn(console, 'error')
    const warn = vi.spyOn(console, 'warn')
    for (const key of Object.keys(flatten(i18n.global.messages.value.en))) {
      expect(i18n.global.t(key, { field: 'Username', name: 'Alex', value: 25, start: '2026-09-01', end: '2026-09-14', timezone: 'UTC', time: '12:00', n: 1, total: 2, count: 3, detail: 'OK', created: 1, updated: 2, unchanged: 3, line: 4 }), key).not.toBe(key)
    }
    expect(error).not.toHaveBeenCalled()
    expect(warn).not.toHaveBeenCalled()
  })

  it('persists English and restores it after reloading the module', async () => {
    setLocale('en')
    expect(getLocale()).toBe('en')
    expect(document.documentElement.lang).toBe('en')
    expect(localStorage.getItem('coreman.locale')).toBe('en')
    vi.resetModules()
    const reloaded = await import('@/i18n')
    expect(reloaded.getLocale()).toBe('en')
  })

  it('switches Element Plus components between all three languages', async () => {
    const wrapper = mount(App, { global: { plugins: [i18n], components: { ElConfigProvider }, stubs: { RouterView: true } } })
    for (const [locale, name, lang] of [['en', 'en', 'en'], ['ja', 'ja', 'ja'], ['zh', 'zh-cn', 'zh-CN']] as const) {
      setLocale(locale)
      await nextTick()
      expect(wrapper.findComponent(ElConfigProvider).props('locale')?.name).toBe(name)
      expect(document.documentElement.lang).toBe(lang)
    }
    wrapper.unmount()
  })

  it('keeps switching usable when storage is unavailable', () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('Storage unavailable') })
    expect(() => setLocale('en')).not.toThrow()
    expect(i18n.global.t('common.save')).toBe('Save')
  })
})
