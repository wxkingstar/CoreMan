import { errorMessage } from '@/utils/errors'
import { ElMessage } from 'element-plus'
import { reactive, ref, toRef, type Ref } from 'vue'
import type { Page } from '@/api/types'
import { useListQuery } from './useListQuery'

export function usePaged<T, F extends Record<string, unknown> = Record<string, unknown>>(
  fetcher: (q: { page: number; per_page: number } & F) => Promise<Page<T>>,
  initialFilters: F,
) {
  const items: Ref<T[]> = ref([])
  const total = ref(0), page = ref(1), perPage = ref(50)
  const loading = ref(false), error = ref('')
  const filters = reactive({ ...initialFilters }) as F
  let generation = 0
  const { persist } = useListQuery({ page, per_page: perPage, ...Object.fromEntries(Object.keys(filters).map(k => [k, toRef(filters, k)])) }, () => { void load() })
  async function load() {
    const request = ++generation
    persist()
    loading.value = true
    error.value = ''
    try {
      const res = await fetcher({ page: page.value, per_page: perPage.value, ...filters })
      if (request !== generation) return
      items.value = res.items
      total.value = res.total
    } catch (e) {
      if (request !== generation) return
      error.value = errorMessage(e)
      items.value = []
      total.value = 0
      ElMessage.error(error.value)
    } finally {
      if (request === generation) loading.value = false
    }
  }
  function reset() { Object.assign(filters, initialFilters); page.value = 1 }
  return { items, total, page, perPage, loading, error, filters, load, reset }
}
