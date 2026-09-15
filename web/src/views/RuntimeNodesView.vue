<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { runtimeNodes, type RuntimeNode, type InstallLink } from '@/api/runtimeNodes'
import { relays, teams } from '@/api/admin'
import type { TeamOut, RelayOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import ProviderBadge from '@/components/ProviderBadge.vue'
import RelayLiveStatus from '@/components/RelayLiveStatus.vue'
import HealthTag from '@/components/HealthTag.vue'
import CatalogPanel from '@/views/CatalogPanel.vue'
import { formatDateTime } from '@/utils/format'
const { t } = useI18n()
const auth = useAuthStore()
const canManage = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))
const nodes = ref<RuntimeNode[]>([])
const teamList = ref<TeamOut[]>([])
const query = ref('')
const tab = ref('nodes')
const loading = ref(false)
const creating = ref(false)
const dialog = ref(false)
const renameDialog = ref(false)
const renameId = ref('')
const renameName = ref('')
const renaming = ref(false)
function openRename(node: RuntimeNode) {
  renameId.value = node.id
  renameName.value = node.name
  renameDialog.value = true
}
async function saveName() {
  const name = renameName.value.trim()
  if (!name || name.length > 100 || renaming.value) return
  renaming.value = true
  try {
    await runtimeNodes.patch(renameId.value, { name })
    const node = nodes.value.find(n => n.id === renameId.value)
    if (node) node.name = name
    renameDialog.value = false
    ElMessage.success(t('common.saved'))
    await refresh()
  } catch (error) { ElMessage.error(errorMessage(error)) }
  finally { renaming.value = false }
}
const generated = ref<InstallLink | null>(null)
const form = reactive({ name: '', workspace_root: '', team_id: null as string | null,
  options: { control_proxy: '', environment: 'auto', proxy: '', claude_path: '', codex_path: '', max_concurrent: 10, install_claude_probe: false } })
const filtered = computed(() => nodes.value.filter(n => `${n.name} ${n.hostname} ${n.username} ${n.workspace_root}`.toLowerCase().includes(query.value.toLowerCase())))
let timer: ReturnType<typeof setInterval> | undefined
async function refresh(silent = false) {
  if (loading.value) return
  loading.value = true
  try {
    nodes.value = await runtimeNodes.list()
  } catch (error) {
    if (!silent) ElMessage.error(errorMessage(error))
  } finally { loading.value = false }
}
async function update(node: RuntimeNode, body: { is_active?: boolean; draining?: boolean }) {
  try { await runtimeNodes.patch(node.id, body); await refresh() }
  catch (error) { ElMessage.error(errorMessage(error)) }
}
async function create() {
  if (!form.workspace_root.startsWith('/') || form.workspace_root === '/') {
    ElMessage.warning(t('runtimeNodes.rootRequired')); return
  }
  creating.value = true
  try { generated.value = await runtimeNodes.createLink(form); await refresh() }
  catch (error) { ElMessage.error(errorMessage(error)) }
  finally { creating.value = false }
}
async function copy() {
  try { await navigator.clipboard.writeText(generated.value?.command ?? ''); ElMessage.success(t('common.copied')); dialog.value = false }
  catch { ElMessage.info(t('runtimeNodes.copyManually')) }
}
async function probe(row: RelayOut) {
  try { await relays.probe(row.id); await refresh() }
  catch (error) { ElMessage.error(errorMessage(error)) }
}
function capabilityState(node: RuntimeNode, provider: string) {
  const cap = node.capabilities[provider]
  if (!cap?.installed) return t('runtimeNodes.notInstalled')
  if (!node.online) return t('runtimeNodes.offline')
  return t(`runtimeNodes.login.${cap.login}`)
}
onMounted(async () => {
  await refresh()
  if (canManage.value) {
    try { teamList.value = await teams.list() } catch { /* node list remains usable */ }
  }
  timer = setInterval(() => { void refresh(true) }, 10000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>
<template>
  <div class="runtime-nodes-view">
    <header class="page-header">
      <div>
        <h2>{{ t('runtimeNodes.title') }}</h2><p class="cm-page-intro">
          {{ t('runtimeNodes.intro') }}
        </p>
      </div>
      <el-button
        v-if="canManage"
        type="primary"
        data-test="install-runtime"
        @click="dialog = true; generated = null"
      >
        {{ t('runtimeNodes.install') }}
      </el-button>
    </header>
    <el-tabs v-model="tab">
      <el-tab-pane
        :label="t('runtimeNodes.nodes')"
        name="nodes"
      >
        <div class="toolbar">
          <el-input
            v-model="query"
            clearable
            :placeholder="t('runtimeNodes.search')"
          /><el-button @click="refresh()">
            {{ t('common.refresh') }}
          </el-button>
        </div>
        <el-empty
          v-if="!nodes.length && !loading"
          :description="t('runtimeNodes.empty')"
        >
          <el-button
            v-if="canManage"
            type="primary"
            @click="dialog = true; generated = null"
          >
            {{ t('runtimeNodes.install') }}
          </el-button>
        </el-empty>
        <div
          v-else
          class="table-scroll"
        >
          <el-table
            v-loading="loading"
            :data="filtered"
            row-key="id"
          >
            <el-table-column type="expand">
              <template #default="{ row }: { row: RuntimeNode }">
                <div class="node-detail">
                  <p>{{ t('runtimeNodes.workspace') }}: <code>{{ row.workspace_root }}</code></p>
                  <p>{{ t('runtimeNodes.version') }}: {{ row.version }} · {{ t('runtimeNodes.service') }}: {{ row.service_status }} · {{ t('runtimeNodes.heartbeat') }}: {{ formatDateTime(row.heartbeat_at) }}</p>
                  <el-alert
                    v-if="['supervised', 'systemd-user-session'].includes(row.service_status)"
                    type="warning"
                    :closable="false"
                    :title="t(row.service_status === 'supervised' ? 'runtimeNodes.supervised' : 'runtimeNodes.sessionService')"
                  />
                  <div
                    v-for="backend in row.backends"
                    :key="backend.id"
                    class="backend-detail"
                  >
                    <ProviderBadge :provider="backend.model_provider" /><HealthTag :status="backend.health_status" />
                    <span>{{ t('runtimeNodes.quota') }}: 5h {{ backend.rate_limit_5h_used_pct == null ? '—' : `${backend.rate_limit_5h_used_pct}%` }} / 7d {{ backend.rate_limit_7d_used_pct == null ? '—' : `${backend.rate_limit_7d_used_pct}%` }}</span>
                    <span>{{ t('runtimeNodes.bots') }}: {{ backend.bot_count }}</span>
                    <RelayLiveStatus
                      v-if="canManage && row.online && row.is_active"
                      :relay-id="backend.id"
                      :name="backend.name"
                    />
                    <el-button
                      v-if="canManage"
                      size="small"
                      :disabled="!row.online || !row.is_active"
                      @click="probe(backend)"
                    >
                      {{ t('relays.probe') }}
                    </el-button>
                    <div class="backend-models">
                      {{ backend.effective_models.join(', ') || t('runtimeNodes.noModels') }}
                    </div>
                    <small>{{ row.capabilities[backend.model_provider]?.detail || backend.health_detail }}</small>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column
              :label="t('runtimeNodes.nodes')"
              min-width="210"
            >
              <template #default="{ row }: { row: RuntimeNode }">
                <strong>{{ row.name }}</strong><div class="muted">
                  {{ row.username }}@{{ row.hostname }}
                </div><div class="muted">
                  {{ row.platform }} / {{ row.architecture }} / {{ row.environment }}
                </div>
              </template>
            </el-table-column>
            <el-table-column
              v-for="provider in ['claude', 'codex']"
              :key="provider"
              min-width="170"
            >
              <template #header>
                <ProviderBadge :provider="provider" />
              </template>
              <template #default="{ row }: { row: RuntimeNode }">
                <span>{{ capabilityState(row, provider) }}</span><div class="muted">
                  {{ row.capabilities[provider]?.version }}
                </div>
              </template>
            </el-table-column>
            <el-table-column
              :label="t('runtimeNodes.team')"
              min-width="110"
            >
              <template #default="{ row }: { row: RuntimeNode }">
                {{ row.team_name ?? t('bots.publicPool') }}
              </template>
            </el-table-column>
            <el-table-column
              :label="t('runtimeNodes.state')"
              width="110"
            >
              <template #default="{ row }: { row: RuntimeNode }">
                <el-tag :type="!row.is_active ? 'info' : row.online ? 'success' : 'danger'">
                  {{ !row.is_active ? t('common.disabled') : row.online ? t('runtimeNodes.online') : t('runtimeNodes.offline') }}
                </el-tag><div
                  v-if="row.draining"
                  class="muted"
                >
                  {{ t('runtimeNodes.draining') }}
                </div>
              </template>
            </el-table-column>
            <el-table-column
              v-if="canManage"
              :label="t('common.actions')"
              min-width="250"
            >
              <template #default="{ row }: { row: RuntimeNode }">
                <el-button
                  text
                  data-test="rename-runtime"
                  @click="openRename(row)"
                >
                  {{ t('runtimeNodes.editName') }}
                </el-button>
                <el-switch
                  :model-value="row.is_active"
                  :aria-label="t('runtimeNodes.enable')"
                  @change="update(row, { is_active: !row.is_active })"
                /><el-button
                  text
                  @click="update(row, { draining: !row.draining })"
                >
                  {{ row.draining ? t('runtimeNodes.resume') : t('runtimeNodes.drain') }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane
        :label="t('relays.tabCatalog')"
        name="catalog"
      >
        <CatalogPanel />
      </el-tab-pane>
    </el-tabs>
    <el-dialog
      v-model="renameDialog"
      :title="t('runtimeNodes.editName')"
      width="min(460px, 95vw)"
      :close-on-click-modal="false"
      :close-on-press-escape="!renaming"
      :show-close="!renaming"
      destroy-on-close
    >
      <el-form
        label-position="top"
        @submit.prevent="saveName"
      >
        <el-form-item
          :label="t('runtimeNodes.name')"
          required
        >
          <el-input
            v-model="renameName"
            data-test="runtime-name"
            maxlength="100"
            show-word-limit
            :disabled="renaming"
          />
        </el-form-item>
        <p class="muted">
          {{ t('runtimeNodes.renameHint') }}
        </p>
      </el-form>
      <template #footer>
        <el-button
          :disabled="renaming"
          @click="renameDialog = false"
        >
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-runtime-name"
          :loading="renaming"
          :disabled="!renameName.trim() || renameName.trim().length > 100"
          @click="saveName"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
    <el-dialog
      v-model="dialog"
      :title="t('runtimeNodes.install')"
      width="min(680px, 95vw)"
      destroy-on-close
      @closed="generated = null"
    >
      <template v-if="!generated">
        <p>{{ t('runtimeNodes.installHint') }}</p>
        <el-form
          label-position="top"
          @submit.prevent="create"
        >
          <el-form-item
            :label="t('runtimeNodes.workspace')"
            required
          >
            <el-input
              v-model="form.workspace_root"
              placeholder="/home/ai/projects"
              data-test="runtime-root"
            /><small>{{ t('runtimeNodes.rootHint') }}</small>
          </el-form-item>
          <el-form-item :label="t('runtimeNodes.name')">
            <el-input
              v-model="form.name"
              :placeholder="t('runtimeNodes.nameAuto')"
            />
          </el-form-item>
          <el-form-item :label="t('runtimeNodes.team')">
            <el-select
              v-model="form.team_id"
              clearable
              :placeholder="t('bots.publicPool')"
            >
              <el-option
                v-for="team in teamList"
                :key="team.id"
                :value="team.id"
                :label="team.name_zh"
              />
            </el-select>
          </el-form-item>
          <p class="muted">
            {{ t('runtimeNodes.validityHint') }}
          </p>
          <el-collapse>
            <el-collapse-item
              :title="t('runtimeNodes.advanced')"
              name="advanced"
            >
              <el-form-item :label="t('runtimeNodes.proxy')">
                <el-input
                  v-model="form.options.proxy"
                  placeholder="http://127.0.0.1:7890"
                />
                <p class="muted proxy-hint">
                  {{ t('runtimeNodes.proxyHint') }}
                </p>
              </el-form-item>
              <el-form-item :label="t('runtimeNodes.controlProxy')">
                <el-input
                  v-model="form.options.control_proxy"
                  placeholder="http://127.0.0.1:7890"
                />
                <p class="muted proxy-hint">
                  {{ t('runtimeNodes.controlProxyHint') }}
                </p>
              </el-form-item>
              <el-form-item :label="t('runtimeNodes.environment')">
                <el-select v-model="form.options.environment">
                  <el-option
                    v-for="item in ['auto', 'host', 'chroot', 'nspawn']"
                    :key="item"
                    :value="item"
                    :label="item === 'auto' ? t('runtimeNodes.autoDiscover') : item"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="Claude CLI">
                <el-input
                  v-model="form.options.claude_path"
                  :placeholder="t('runtimeNodes.autoDiscover')"
                />
              </el-form-item>
              <el-form-item label="Codex CLI">
                <el-input
                  v-model="form.options.codex_path"
                  :placeholder="t('runtimeNodes.autoDiscover')"
                />
              </el-form-item>
              <el-form-item :label="t('runtimeNodes.concurrency')">
                <el-input-number
                  v-model="form.options.max_concurrent"
                  :min="1"
                  :max="32"
                />
              </el-form-item>
              <el-checkbox v-model="form.options.install_claude_probe">
                {{ t('runtimeNodes.claudeProbe') }}
              </el-checkbox><p class="muted">
                {{ t('runtimeNodes.probeHint') }}
              </p>
            </el-collapse-item>
          </el-collapse>
        </el-form>
      </template>
      <template v-else>
        <el-alert
          type="success"
          :closable="false"
          :title="t('runtimeNodes.linkReady')"
        /><p>{{ t('runtimeNodes.commandHint') }}</p><el-input
          :model-value="generated.command"
          type="textarea"
          :rows="4"
          readonly
          data-test="runtime-install-command"
        /><p>{{ t('runtimeNodes.expires') }}: {{ formatDateTime(generated.expires_at) }}</p><small>{{ t('runtimeNodes.credentialHint') }}</small>
      </template>
      <template #footer>
        <el-button @click="dialog = false">
          {{ t('common.close') }}
        </el-button><el-button
          v-if="!generated"
          type="primary"
          :loading="creating"
          data-test="create-runtime-link"
          @click="create"
        >
          {{ t('runtimeNodes.createLink') }}
        </el-button><el-button
          v-else
          type="primary"
          @click="copy"
        >
          {{ t('runtimeNodes.copyCommand') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
<style scoped>
.page-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.page-header h2 { margin: 0 0 8px; }
.toolbar { display: flex; gap: 12px; margin: 12px 0 20px; max-width: 520px; }
.muted, small { color: var(--el-text-color-secondary); font-size: 12px; }
.proxy-hint { width: 100%; margin: 6px 0 0; line-height: 1.6; overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; }
.node-detail { padding: 10px 28px 24px; }
.backend-detail { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 18px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.backend-models { width: 100%; overflow-wrap: anywhere; font-size: 13px; }
@media (max-width: 640px) { .page-header { align-items: flex-start; flex-direction: column; } .node-detail { padding: 12px; } }
</style>
