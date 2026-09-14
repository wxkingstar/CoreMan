import { createI18n } from 'vue-i18n'
import ja from './ja'
import en from './en'
import skillEditorEn from './skillEditorEn'
import zh from './zh'
import { skillEditorZh, skillEditorJa } from './skillEditor'

export type Locale = 'zh' | 'ja' | 'en'
const STORAGE_KEY = 'coreman.locale'

function saved(): Locale {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v === 'ja' || v === 'en' ? v : 'zh'
  } catch {
    return 'zh'
  }
}

export const i18n = createI18n({ legacy: false, locale: saved(), fallbackLocale: 'zh', messages: { zh: { ...zh, skillEditor: skillEditorZh }, ja: { ...ja, skillEditor: skillEditorJa }, en: { ...en, skillEditor: skillEditorEn } } })

export function getLocale(): Locale {
  return i18n.global.locale.value as Locale
}

export function setLocale(l: Locale): void {
  i18n.global.locale.value = l
  document.documentElement.lang = l === 'zh' ? 'zh-CN' : l
  try {
    localStorage.setItem(STORAGE_KEY, l)
  } catch {
    /* 私密模式等无 localStorage 时忽略 */
  }
}
