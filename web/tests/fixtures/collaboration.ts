// Local visual fixture: every HTTP request is intercepted; no real API or IM traffic.
import { createApp, h } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../../src/styles/theme.css'
import '../../src/styles/controls.css'
import '../../src/styles/overlays.css'
import { i18n } from '../../src/i18n'
import { http } from '../../src/api/client'
import BotCollaborators from '../../src/components/BotCollaborators.vue'
import type { CollaborationRoute } from '../../src/api/collaboration'
const rows: CollaborationRoute[] = []
let pendingSince = 0
http.defaults.adapter = async config => {
 const url = config.url ?? ''
 let data: unknown
 if (url.endsWith('/collaborator-options')) data = [{ id: 'peer', name: '财务助手', description: '负责核对费用和结算', enabled: true, available: true }]
 else if (url.endsWith('/collaborator-groups')) data = [{ chat_id: 'group', name: '运营与财务协作群' }]
 else if (config.method === 'post') {
  const row: CollaborationRoute = { id: 'route', target_bot_id: 'peer', target_name: '财务助手', target_description: '负责核对费用和结算', chat_id: 'group', chat_name: '运营与财务协作群', enabled: false, version: 1, status: 'pending', reason: null, can_enable: false, can_verify: false, can_remove: true, active_count: 0 }
  rows.splice(0, rows.length, row); pendingSince = Date.now(); data = { ...row }
 } else if (config.method === 'patch') {
  const enabled = JSON.parse(config.data).enabled; Object.assign(rows[0], { enabled, version: rows[0].version + 1 }); data = { ...rows[0] }
 } else if (config.method === 'delete') { rows.splice(0); data = null }
 else {
  if (rows[0]?.status === 'pending' && Date.now() - pendingSince > 4000) Object.assign(rows[0], { status: 'ready', can_enable: true, version: 2 })
  data = rows.map(row => ({ ...row }))
 }
 return { data: { code: 0, data }, status: 200, statusText: 'OK', headers: {}, config }
}
createApp({ render: () => h('main', { style: 'max-width:1080px;margin:32px auto;padding:16px;' }, [h('h2', '运营助手 · 协作伙伴'), h(BotCollaborators, { botId: 'fixture', sourceName: '运营助手', canEdit: true, active: true })]) }).use(ElementPlus).use(i18n).mount('#app')
