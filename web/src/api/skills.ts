import { call, http } from './client'

export interface Source { has_access_token?: boolean; id: string; key: string; label: string; git_url: string | null; categories: Record<string, string>; sort_order: number; version: number }
export interface SkillInput {
  name: string; source_id: string; description: string; category: string | null; security_level: 'public' | 'internal'; version: string | null
  env_groups: string[]; selectable_env_groups: Record<string, string>; data_sources: Record<string, string> | null
  default_data_source: string | null; doris_enabled_groups: string[]; user_env_vars: Record<string, { label: string; placeholder: string; required: boolean }>
  install_type: 'git' | 'mcp'; external_repo_url: string | null; mcp_config?: Record<string, unknown> | null; security_prompt_template: string | null; enabled: boolean
}
export interface Skill extends SkillInput { id: string; revision: number; has_mcp_config: boolean }
export interface Preset { group_key: string; label: string; vars: Record<string, string>; tags: string[]; version: number }
export interface Installed { skill_id: string; name: string; status: string; revision: number; version: string | null; selected_env_groups: string[]; user_env_vars: Record<string, string>; security_prompt: string | null; error_message: string | null }
export interface Approval { bot_name?: string | null; skill_revision?: number; id: string; bot_id: string; skill_id: string; requested_databases: string[]; requested_security_prompt: string; approved_databases: string[] | null; status: string; version: number; requested_at: string; requested_by: string; reviewed_by: string | null; review_comment: string | null }
export interface InstallInput { selected_env_groups: string[]; data_source: string | null; user_env_vars: Record<string, string>; requested_security_prompt: string | null; reinstall_code: boolean }
type Page<T> = { items: T[]; total: number }
const root = '/api/admin'
const match = (version: number) => ({ headers: { 'If-Match': String(version) } })
export const skills = {
  list: (page = 1) => call<Page<Skill>>(http.get(`${root}/skills`, { params: { page, per_page: 200 } })),
  sources: () => call<Source[]>(http.get(`${root}/skill-sources`)),
  sourceSync: (row: Source) => call<{ created: number; updated: number; unchanged: number }>(http.post(`${root}/skill-sources/${row.id}/sync`, {}, { ...match(row.version), timeout: 210000 })),
  sourceSave: (row: Source | null, body: Omit<Source, 'id' | 'version' | 'has_access_token'> & { access_token?: string; remove_access_token?: boolean }) => call<Source>(row ? http.put(`${root}/skill-sources/${row.id}`, body, match(row.version)) : http.post(`${root}/skill-sources`, body)),
  save: (row: Skill | null, body: SkillInput) => call<Skill>(row ? http.put(`${root}/skills/${row.id}`, body, match(row.revision)) : http.post(`${root}/skills`, body)),
  presets: () => call<Preset[]>(http.get(`${root}/env-presets`)),
  presetSave: (body: Preset) => call<Preset>(http.put(`${root}/env-presets/${body.group_key}`, { group_key: body.group_key, label: body.label, vars: body.vars, tags: body.tags }, match(body.version))),
  installed: (bot: string) => call<{ items: Installed[]; pending_approvals: Approval[] }>(http.get(`${root}/bots/${bot}/skills`)),
  install: (bot: string, skill: string, body: InstallInput) => call<{ status: string }>(http.post(`${root}/bots/${bot}/skills/${skill}/install`, body)),
  uninstall: (bot: string, row: Installed) => call(http.delete(`${root}/bots/${bot}/skills/${row.skill_id}`, match(row.revision))),
  approvals: (page = 1) => call<Page<Approval>>(http.get(`${root}/skill-approvals`, { params: { page, per_page: 50 } })),
  review: (row: Approval, body: { decision: 'approve' | 'reject'; approved_databases: string[]; approved_security_prompt: string; comment: string }) => call(http.post(`${root}/skill-approvals/${row.id}/review`, body, match(row.version))),
}
export async function allSkills() {
  const rows: Skill[] = []
  for (let page = 1; ; page++) { const data = await skills.list(page); rows.push(...data.items); if (!data.items.length || rows.length >= data.total) return rows }
}
