import { getCurrentInstance, inject, watch, type Ref } from 'vue'
import { routeLocationKey, routerKey, type LocationQueryRaw } from 'vue-router'

/** Persist only list controls, never resource data or form contents. URL takes precedence. */
export function useListQuery(fields: Record<string, Ref<unknown>>, reload?: () => void) {
  const instance = getCurrentInstance()
  const route = instance ? inject(routeLocationKey, undefined) : undefined
  const router = instance ? inject(routerKey, undefined) : undefined
  const path = route?.path
  const defaults = Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, value.value]))
  const storageKey = `coreman.list:${path}:${Object.keys(fields).sort().join(',')}`
  function restore(query: Record<string, unknown>) {
    for (const [key, target] of Object.entries(fields)) {
      const raw = query[key]
      const fallback = defaults[key]
      if (raw == null || Array.isArray(raw)) { target.value = fallback; continue }
      if (typeof fallback === 'number') { const n = Number(raw); target.value = Number.isSafeInteger(n) && n > 0 && n <= (key === 'per_page' ? 200 : 1000000) ? n : fallback }
      else if (typeof fallback === 'boolean' || ['enabled', 'is_active', 'is_disabled', 'unassigned'].includes(key)) target.value = raw === 'true' ? true : raw === 'false' ? false : fallback
      else if (Array.isArray(fallback)) { try { const parsed: unknown = JSON.parse(String(raw)); target.value = Array.isArray(parsed) && parsed.every(v => typeof v === 'string') ? parsed : fallback } catch { target.value = fallback } }
      else target.value = String(raw)
    }
  }
  if (route) {
    let initial: Record<string, unknown> = route.query
    if (!Object.keys(route.query).length) {
      try { initial = JSON.parse(sessionStorage.getItem(storageKey) || '{}') } catch { /* URL still works without storage */ }
    }
    restore(initial)
  }
  function persist() {
    if (!route || !router || route.path !== path) return
    const query: LocationQueryRaw = { ...route.query }
    for (const [key, field] of Object.entries(fields)) {
      const value = field.value
      if (value == null || value === '') delete query[key]
      else query[key] = Array.isArray(value) ? JSON.stringify(value) : String(value)
    }
    try { sessionStorage.setItem(storageKey, JSON.stringify(query)) } catch { /* no persistent storage */ }
    if (JSON.stringify(query) !== JSON.stringify(route.query)) void router.replace({ query })
  }
  if (route) watch(() => route.query, (query) => {
    if (route.path !== path) return
    const before = JSON.stringify(Object.values(fields).map(v => v.value))
    restore(query)
    if (before !== JSON.stringify(Object.values(fields).map(v => v.value))) reload?.()
  })
  return { persist }
}
