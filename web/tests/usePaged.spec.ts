import { flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { usePaged } from '@/composables/usePaged'

describe('usePaged', () => {
  it('loads pages with filters and tracks total', async () => {
    const fetcher = vi.fn().mockResolvedValue({ items: ['a', 'b'], total: 12, page: 2, per_page: 2 })
    const p = usePaged<string>(fetcher, { keyword: '' })
    p.filters.keyword = 'x'
    p.page.value = 2
    p.perPage.value = 2
    await p.load()
    await flushPromises()
    expect(fetcher).toHaveBeenCalledWith({ page: 2, per_page: 2, keyword: 'x' })
    expect(p.items.value).toEqual(['a', 'b'])
    expect(p.total.value).toBe(12)
    p.reset()
    expect(p.page.value).toBe(1)
    expect(p.filters.keyword).toBe('')
  })
})

it('ignores stale responses when a later search finishes first', async () => {
  let resolveFirst!: (value: { items: string[]; total: number }) => void
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve })).mockResolvedValueOnce({ items: ['new'], total: 1 })
  const p = usePaged<string>(fetcher, {})
  const old = p.load()
  await p.load()
  resolveFirst({ items: ['old'], total: 100 })
  await old
  expect(p.items.value).toEqual(['new'])
  expect(p.total.value).toBe(1)
})
