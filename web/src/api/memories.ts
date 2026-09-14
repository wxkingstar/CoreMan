import { call, http } from './client'

export interface MemoryRow {
  id: string; bot_id: string; file_name: string; name: string | null; description: string | null
  type: string | null; content?: string; content_hash: string; file_mtime: string | null
  collected_from: string | null; deleted_at: string | null; version: number
}
export interface MemoryInput { file_name: string; content: string; name?: string | null; description?: string | null; type?: string | null }
const base = (id: string) => `/api/admin/bots/${id}/memories`
export const memories = {
  list: (id: string, deleted: boolean) => call<MemoryRow[]>(http.get(base(id), { params: { include_deleted: deleted } })),
  get: (id: string, row: string) => call<MemoryRow>(http.get(`${base(id)}/${row}`)),
  create: (id: string, body: MemoryInput) => call<MemoryRow>(http.post(base(id), body)),
  update: (id: string, row: string, body: MemoryInput, version: number) => call<MemoryRow>(http.put(`${base(id)}/${row}`, body, { headers: { 'If-Match': String(version) } })),
  remove: (id: string, row: string, version: number) => call<MemoryRow>(http.delete(`${base(id)}/${row}`, { headers: { 'If-Match': String(version) } })),
  collect: (id: string) => call<{ count: number }>(http.post(`${base(id)}/collect`, {}, { timeout: 150000 })),
  deploy: (id: string) => call<{ count: number }>(http.post(`${base(id)}/deploy`, {}, { timeout: 150000 })),
}
