import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, http } from '@/api/client'
import { myAssistant } from '@/api/myAssistant'

afterEach(() => { vi.restoreAllMocks() })

describe('import and export calls', () => {
  it('uploads the archive as a form with the preview flags and a long timeout', async () => {
    const post = vi.spyOn(http, 'post').mockResolvedValue({ data: { code: 0, data: { digest: 'd' } } } as never)
    const file = new File(['zip'], 'notes.zip', { type: 'application/zip' })
    await myAssistant.importArchive(file, { dryRun: true, overwrite: false })
    await myAssistant.importArchive(file, { dryRun: false, overwrite: true, expect: 'abc' })
    const [url, first, config] = post.mock.calls[0] as unknown as [string, FormData, { timeout: number }]
    expect(url).toBe('/api/me/assistant/import')
    expect(first.get('archive')).toBeInstanceOf(File)
    expect([first.get('dry_run'), first.get('overwrite'), first.get('expect')]).toEqual(['true', 'false', null])
    expect(config.timeout).toBe(180000)
    const second = post.mock.calls[1][1] as FormData
    expect([second.get('dry_run'), second.get('overwrite'), second.get('expect')]).toEqual(['false', 'true', 'abc'])
  })

  it('downloads the export as a blob and reports a failure with its status', async () => {
    const blob = new Blob(['zip'])
    const get = vi.spyOn(http, 'get').mockResolvedValueOnce({ data: blob } as never)
    expect(await myAssistant.exportArchive()).toBe(blob)
    expect(get.mock.calls[0]).toEqual(['/api/me/assistant/export', { responseType: 'blob', timeout: 180000 }])
    get.mockRejectedValueOnce({ response: { status: 409 }, message: 'Request failed with status code 409' })
    const failure = await myAssistant.exportArchive().catch((e: unknown) => e)
    expect(failure).toBeInstanceOf(ApiError)
    expect((failure as ApiError).status).toBe(409)
  })
})
