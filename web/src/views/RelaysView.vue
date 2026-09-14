<script setup lang="ts">
import QuotaMeter from '@/components/QuotaMeter.vue'
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { catalog, relays, teams as teamsApi } from '@/api/admin'
import type { CatalogOut, RelayIn, RelayModels, RelayOut, TeamLoad, TeamOut } from '@/api/types'
import HealthTag from '@/components/HealthTag.vue'
import RelayLiveStatus from '@/components/RelayLiveStatus.vue'
import { usePaged } from '@/composables/usePaged'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/format'
import CatalogPanel from '@/views/CatalogPanel.vue'

const { t } = useI18n()
const auth = useAuthStore()
const canManage = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))

const RUNTIME_ENVS = ['host', 'chroot', 'nspawn'] as const

const tab = ref<'relays' | 'catalog' | 'load'>('relays')

type RelayFilters = { model_provider: string | null; team_id: string | null; is_active: boolean | null }
const paged = usePaged<RelayOut, RelayFilters>((q) => relays.list(q), {
  model_provider: null,
  team_id: null,
  is_active: null,
})

const teamList = ref<TeamOut[]>([])
const teamLoad = ref<TeamLoad | null>(null)
const probing = reactive<Record<string, boolean>>({})

function fail(e: unknown) {
  ElMessage.error(e instanceof Error ? e.message : String(e))
}

function search() {
  paged.page.value = 1
  paged.load()
}

function resetFilters() {
  paged.reset()
  paged.load()
}

function onPageChange(p: number) {
  paged.page.value = p
  paged.load()
}

function onSizeChange(size: number) {
  paged.perPage.value = size
  paged.page.value = 1
  paged.load()
}

function toRelayIn(r: RelayOut): RelayIn {
  return {
    name: r.name,
    host: r.host,
    clawrelay_port: r.clawrelay_port,
    agent_port: r.agent_port,
    ssh_user: r.ssh_user,
    runtime_env: r.runtime_env,
    chroot_path: r.chroot_path,
    runtime_user: r.runtime_user,
    model_provider: r.model_provider,
    supported_models_mode: r.supported_models_mode,
    supported_models: r.supported_models,
    team_id: r.team_id,
    visibility: r.visibility,
    description: r.description,
    is_active: r.is_active,
  }
}

async function toggleActive(r: RelayOut) {
  try {
    Object.assign(r, await relays.update(r.id, { ...toRelayIn(r), is_active: !r.is_active }, r.version))
    ElMessage.success(t('common.saved'))
  } catch (e) {
    fail(e)
    await paged.load()
  }
}

async function probe(r: RelayOut) {
  probing[r.id] = true
  try {
    const res = await relays.probe(r.id)
    Object.assign(r, res.relay)
    const ok = res.health.status === 'healthy'
    const msg = t(ok ? 'relays.probeOk' : 'relays.probeFailed', { detail: res.health.detail ?? '' })
    ;(ok ? ElMessage.success : ElMessage.warning)(msg)
  } catch (e) {
    fail(e)
  } finally {
    probing[r.id] = false
  }
}

const modelsDialog = reactive<{ visible: boolean; name: string; data: RelayModels | null }>({
  visible: false,
  name: '',
  data: null,
})

async function openModels(r: RelayOut) {
  try {
    const data = await relays.models(r.id)
    modelsDialog.name = r.name
    modelsDialog.data = data
    modelsDialog.visible = true
  } catch (e) {
    fail(e)
  }
}

const tokenDialog = reactive<{ visible: boolean; token: string }>({ visible: false, token: '' })

async function issueToken(r: RelayOut) {
  try {
    await ElMessageBox.confirm(t('relays.tokenConfirm'), t('relays.token'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    const { token } = await relays.agentToken(r.id)
    tokenDialog.token = token
    tokenDialog.visible = true
    // 签发会 bump version：只改 has_agent_token 的话，下一次编辑/停用带的还是旧 If-Match（409）。
    Object.assign(r, await relays.get(r.id))
  } catch (e) {
    fail(e)
  }
}

async function copyToken() {
  try {
    // 非安全上下文（http）里 navigator.clipboard 是 undefined；写成 `?.` 会静默短路，
    // 然后照样弹「已复制」——必须显式抛出，走下面的失败提示。
    if (!navigator.clipboard) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(tokenDialog.token)
    ElMessage.success(t('relays.copied'))
  } catch {
    // 用户自己选中复制即可，不要抛到控制台。
    ElMessage.warning(t('relays.copyFailed'))
  }
}

async function remove(r: RelayOut) {
  try {
    await ElMessageBox.confirm(t('relays.deleteConfirm'), t('common.delete'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    await relays.remove(r.id)
    ElMessage.success(t('common.deleted'))
    await paged.load()
  } catch (e) {
    // 409「仍有机器人使用该 relay」直接把后端的话给用户看。
    fail(e)
  }
}

function emptyForm(): RelayIn {
  return {
    name: '',
    host: '',
    clawrelay_port: 50009,
    agent_port: null,
    ssh_user: null,
    runtime_env: 'host',
    chroot_path: null,
    runtime_user: null,
    model_provider: 'claude',
    supported_models_mode: 'inherit',
    supported_models: null,
    team_id: null,
    visibility: 'all',
    description: null,
    is_active: true,
  }
}

const dialogVisible = ref(false)
const dialogEditingId = ref<string | null>(null)
const dialogEditingVersion = ref(0)
const form = reactive<RelayIn>(emptyForm())
const modelOptions = ref<CatalogOut[]>([])

async function loadModelOptions() {
  try {
    modelOptions.value = await catalog.list(form.model_provider)
  } catch (e) {
    fail(e)
  }
}

function onModeChange() {
  if (form.supported_models_mode === 'restricted') {
    if (form.supported_models === null) form.supported_models = []
    loadModelOptions()
  }
}

/** 换 provider 时白名单的候选模型也得跟着换，否则还挂着上一个 provider 的目录。 */
function onProviderChange() {
  if (form.supported_models_mode === 'restricted') loadModelOptions()
}

function openCreate() {
  dialogEditingId.value = null
  Object.assign(form, emptyForm())
  modelOptions.value = []
  dialogVisible.value = true
}

function openEdit(r: RelayOut) {
  dialogEditingId.value = r.id
  dialogEditingVersion.value = r.version
  Object.assign(form, toRelayIn(r))
  modelOptions.value = []
  if (form.supported_models_mode === 'restricted') loadModelOptions()
  dialogVisible.value = true
}

async function submitForm() {
  if (!form.name || !form.host || !form.model_provider) {
    ElMessage.error(t('login.required', { field: t('relays.name') }))
    return
  }
  const body: RelayIn = { ...form, supported_models: form.supported_models_mode === 'restricted' ? form.supported_models : null }
  try {
    if (dialogEditingId.value) await relays.update(dialogEditingId.value, body, dialogEditingVersion.value)
    else await relays.create(body)
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    await paged.load()
  } catch (e) {
    fail(e)
  }
}

async function fetchTeamLoad() {
  try {
    teamLoad.value = await relays.teamLoad()
  } catch (e) {
    fail(e)
  }
}

onMounted(async () => {
  await paged.load()
  try {
    teamList.value = await teamsApi.list()
  } catch (e) {
    fail(e)
  }
  await fetchTeamLoad()
})
</script>

<template>
  <div class="relays-view">
    <div class="page-header cm-page-header">
      <h2>{{ t('relays.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.relays') }} {{ t('workspace.staleQuotaHint') }}
      </p>
      <el-button
        v-if="canManage"
        type="primary"
        data-test="create-relay"
        @click="openCreate"
      >
        {{ t('common.create') }}
      </el-button>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane
        :label="t('relays.tabRelays')"
        name="relays"
      >
        <el-form
          :inline="true"
          @submit.prevent
        >
          <el-form-item :label="t('relays.provider')">
            <el-input
              v-model="paged.filters.model_provider"
              clearable
              style="width: 140px"
            />
          </el-form-item>
          <el-form-item :label="t('relays.team')">
            <el-select
              v-model="paged.filters.team_id"
              clearable
              :placeholder="t('relays.team')"
              style="width: 180px"
            >
              <el-option
                v-for="tm in teamList"
                :key="tm.id"
                :label="tm.name_zh"
                :value="tm.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('relays.active')">
            <el-select
              v-model="paged.filters.is_active"
              clearable
              :placeholder="t('relays.active')"
              style="width: 120px"
            >
              <el-option
                :label="t('common.enabled')"
                :value="true"
              />
              <el-option
                :label="t('common.disabled')"
                :value="false"
              />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              @click="search"
            >
              {{ t('common.search') }}
            </el-button>
            <el-button @click="resetFilters">
              {{ t('common.reset') }}
            </el-button>
          </el-form-item>
        </el-form>

        <LoadState
          :error="paged.error.value"
          @retry="paged.load"
        />
        <el-table
          v-loading="paged.loading.value"
          :data="paged.items.value"
        >
          <el-table-column
            min-width="140"
            prop="name"
            :label="t('relays.name')"
          />
          <el-table-column
            min-width="140"
            :label="t('relays.provider')"
          >
            <template #default="{ row }: { row: RelayOut }">
              <el-tag size="small">
                {{ row.model_provider }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('relays.server')"
          >
            <template #default="{ row }: { row: RelayOut }">
              <div>{{ row.ssh_user ? row.ssh_user + '@' + row.host : row.host }}</div>
              <el-tag
                size="small"
                type="info"
              >
                {{ row.runtime_env }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('relays.ports')"
          >
            <template #default="{ row }: { row: RelayOut }">
              <span>CL:{{ row.clawrelay_port }} → AG:{{ row.agent_port ?? '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('relays.rate5h')"
            width="130"
          >
            <template #default="{ row }: { row: RelayOut }">
              <QuotaMeter
                :value="row.rate_limit_5h_used_pct"
                :collected-at="row.rate_limit_probed_at"
                :resets-at="row.rate_limit_5h_resets_at"
              />
            </template>
          </el-table-column>
          <el-table-column
            :label="t('relays.rate7d')"
            width="130"
          >
            <template #default="{ row }: { row: RelayOut }">
              <QuotaMeter
                :value="row.rate_limit_7d_used_pct"
                :collected-at="row.rate_limit_probed_at"
                :resets-at="row.rate_limit_7d_resets_at"
              />
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('relays.healthTitle')"
          >
            <template #default="{ row }: { row: RelayOut }">
              <el-tooltip
                v-if="row.health_detail"
                :content="row.health_detail"
                placement="top"
              >
                <HealthTag :status="row.health_status" />
              </el-tooltip>
              <HealthTag
                v-else
                :status="row.health_status"
              />
              <div class="muted">
                {{ formatDateTime(row.health_checked_at) }}
              </div>
            </template>
          </el-table-column>
          <el-table-column
            prop="bot_count"
            :label="t('relays.bots')"
            width="80"
          />
          <el-table-column
            :label="t('relays.active')"
            width="90"
          >
            <template #default="{ row }: { row: RelayOut }">
              <el-switch
                v-if="canManage"
                :data-test="'active-' + row.id"
                :model-value="row.is_active"
                @change="toggleActive(row)"
              />
              <el-tag
                v-else
                size="small"
                :type="row.is_active ? 'success' : 'info'"
              >
                {{ row.is_active ? t('common.enabled') : t('common.disabled') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('common.actions')"
            width="430"
          >
            <template #default="{ row }: { row: RelayOut }">
              <RelayLiveStatus
                v-if="canManage && row.has_agent_token && row.agent_port"
                :relay-id="row.id"
                :name="row.name"
              />
              <el-button
                v-if="canManage"
                size="small"
                :loading="!!probing[row.id]"
                :data-test="'probe-' + row.id"
                @click="probe(row)"
              >
                {{ t('relays.probe') }}
              </el-button>
              <el-button
                size="small"
                :data-test="'models-' + row.id"
                @click="openModels(row)"
              >
                {{ t('relays.models') }}
              </el-button>
              <el-button
                v-if="canManage"
                size="small"
                :data-test="'token-' + row.id"
                @click="issueToken(row)"
              >
                {{ t('relays.token') }}
              </el-button>
              <el-button
                v-if="canManage"
                size="small"
                :data-test="'edit-' + row.id"
                @click="openEdit(row)"
              >
                {{ t('common.edit') }}
              </el-button>
              <el-button
                v-if="canManage"
                size="small"
                type="danger"
                :data-test="'delete-' + row.id"
                @click="remove(row)"
              >
                {{ t('common.delete') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-pagination
          :total="paged.total.value"
          :current-page="paged.page.value"
          :page-size="paged.perPage.value"
          :page-sizes="[20, 50, 100, 200]"
          layout="total, prev, pager, next, sizes"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </el-tab-pane>

      <el-tab-pane
        :label="t('relays.tabCatalog')"
        name="catalog"
      >
        <CatalogPanel />
      </el-tab-pane>

      <el-tab-pane
        :label="t('relays.tabLoad')"
        name="load"
      >
        <el-alert
          type="info"
          :closable="false"
          show-icon
        >
          {{ t('relays.load.unassignedUsers') }}: {{ teamLoad?.unassigned_user_count ?? 0 }} ·
          {{ t('relays.load.unassignedBots') }}: {{ teamLoad?.unassigned_bot_count ?? 0 }}
        </el-alert>
        <el-table :data="teamLoad?.teams ?? []">
          <el-table-column
            min-width="140"
            prop="team_name"
            :label="t('relays.load.team')"
          />
          <el-table-column
            prop="relay_count"
            :label="t('relays.load.relays')"
            width="100"
          />
          <el-table-column
            min-width="140"
            :label="t('relays.name')"
          >
            <template #default="{ row }: { row: TeamLoad['teams'][number] }">
              <el-tag
                v-for="n in row.relay_names"
                :key="n"
                size="small"
                class="name-tag"
              >
                {{ n }}
              </el-tag>
              <span v-if="!row.relay_names.length">—</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="bot_count"
            :label="t('relays.load.bots')"
            width="100"
          />
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="dialogVisible"
      :close-on-click-modal="false"
      :title="dialogEditingId ? t('common.edit') : t('common.create')"
      width="640px"
      destroy-on-close
    >
      <el-form
        :model="form"
        label-width="140px"
      >
        <el-form-item :label="t('relays.name')">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item :label="t('relays.host')">
          <el-input v-model="form.host" />
        </el-form-item>
        <el-form-item :label="t('relays.clawrelayPort')">
          <el-input-number
            v-model="form.clawrelay_port"
            :min="1"
            :max="65535"
          />
        </el-form-item>
        <el-form-item :label="t('relays.agentPort')">
          <el-input-number
            :model-value="form.agent_port ?? undefined"
            :min="1"
            :max="65535"
            @update:model-value="form.agent_port = $event ?? null"
          />
        </el-form-item>
        <el-form-item :label="t('relays.sshUser')">
          <el-input
            :model-value="form.ssh_user ?? ''"
            @update:model-value="form.ssh_user = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('relays.runtimeEnv')">
          <el-select
            v-model="form.runtime_env"
            style="width: 160px"
          >
            <el-option
              v-for="env in RUNTIME_ENVS"
              :key="env"
              :label="env"
              :value="env"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('relays.chrootPath')">
          <el-input
            :model-value="form.chroot_path ?? ''"
            @update:model-value="form.chroot_path = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('relays.runtimeUser')">
          <el-input
            :model-value="form.runtime_user ?? ''"
            @update:model-value="form.runtime_user = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('relays.provider')">
          <el-input
            v-model="form.model_provider"
            @change="onProviderChange"
          />
        </el-form-item>
        <el-form-item :label="t('relays.modelsMode')">
          <el-radio-group
            v-model="form.supported_models_mode"
            @change="onModeChange"
          >
            <el-radio value="inherit">
              {{ t('relays.modes.inherit') }}
            </el-radio>
            <el-radio value="restricted">
              {{ t('relays.modes.restricted') }}
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          v-if="form.supported_models_mode === 'restricted'"
          :label="t('relays.supportedModels')"
        >
          <el-select
            :model-value="form.supported_models ?? []"
            multiple
            filterable
            allow-create
            style="width: 100%"
            @update:model-value="form.supported_models = $event as string[]"
          >
            <el-option
              v-for="m in modelOptions"
              :key="m.model"
              :label="m.display_name ?? m.model"
              :value="m.model"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('relays.team')">
          <el-select
            v-model="form.team_id"
            clearable
            :placeholder="t('relays.publicPool')"
            style="width: 240px"
          >
            <el-option
              v-for="tm in teamList"
              :key="tm.id"
              :label="tm.name_zh"
              :value="tm.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('relays.visibility')">
          <el-radio-group v-model="form.visibility">
            <el-radio value="all">
              {{ t('relays.visibilities.all') }}
            </el-radio>
            <el-radio value="admins">
              {{ t('relays.visibilities.admins') }}
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="t('relays.description')">
          <el-input
            :model-value="form.description ?? ''"
            type="textarea"
            :rows="2"
            @update:model-value="form.description = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('relays.active')">
          <el-switch v-model="form.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-relay"
          @click="submitForm"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="modelsDialog.visible"
      :title="t('relays.models') + ' · ' + modelsDialog.name"
      width="520px"
    >
      <p class="muted">
        {{ t('relays.defaultModel') }}: {{ modelsDialog.data?.default ?? '—' }}
      </p>
      <p>{{ t('relays.effectiveModels') }}</p>
      <el-tag
        v-for="m in modelsDialog.data?.models ?? []"
        :key="m"
        class="name-tag"
      >
        {{ m }}
      </el-tag>
      <span v-if="!modelsDialog.data?.models.length">—</span>
    </el-dialog>

    <el-dialog
      v-model="tokenDialog.visible"
      :close-on-click-modal="false"
      :title="t('relays.tokenTitle')"
      width="520px"
    >
      <el-alert
        type="warning"
        :title="t('relays.tokenOnce')"
        :closable="false"
        show-icon
      />
      <el-input
        :model-value="tokenDialog.token"
        readonly
        class="token-input"
        data-test="token-value"
      />
      <template #footer>
        <el-button
          type="primary"
          data-test="copy-token"
          @click="copyToken"
        >
          {{ t('relays.copy') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.name-tag { margin: 0 4px 4px 0; }
.token-input { margin-top: 12px; }
</style>
