import { onBeforeUnmount, ref } from 'vue'
export function useCompactLayout() {
  const media = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 767px)') : undefined
  const compact = ref(media?.matches ?? false)
  const update = () => { compact.value = media?.matches ?? false }
  media?.addEventListener('change', update)
  onBeforeUnmount(() => media?.removeEventListener('change', update))
  return compact
}
