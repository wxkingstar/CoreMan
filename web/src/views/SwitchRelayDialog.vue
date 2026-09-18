<script setup lang="ts">
import { errorMessage, isVersionConflict } from '@/utils/errors'
import { ElMessage } from 'element-plus'
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { workspace, type SwitchPreview } from '@/api/workspace'
import { bots, relays } from '@/api/admin'

import type { BotOut, RelayOut, SwitchRelayOut } from '@/api/types'
import HealthTag from '@/components/HealthTag.vue'
import { pctColor, pctNum } from '@/utils/format'
import { backendOf } from '@/utils/models'

const props = defineProps<{ bot: BotOut; visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [boolean]; switched: [SwitchRelayOut] }>()

const { t } = useI18n()

const relayList = ref<RelayOut[]>([])
const loading = ref(false)
const saving = ref(false)
const targetId = ref<string | null>(null)
const workspaceMode = ref<'copy' | 'git' | 'existing'>('copy')
const targetDirectory = ref('')
const allowStoredMemory = ref(false)
const preview = ref<SwitchPreview>()
const checking = ref(false)
const progress = ref('')
let progressTimer: ReturnType<typeof setInterval> | undefined
const moving = computed(() => !!targetId.value && (targetId.value !== props.bot.relay_server_id || targetDirectory.value !== props.bot.working_dir))
// 使用已有目录：本员工的，或没有任何员工认领（如另一套机器人系统在用的目录，直接接管）。
const unclaimable = computed(() => !!preview.value && workspaceMode.value === 'existing' && (!preview.value.exists || (!preview.value.owned && preview.value.marked)))
const takeover = computed(() => !!preview.value && workspaceMode.value === 'existing' && preview.value.exists && !preview.value.owned && !preview.value.marked)
const targetBlocked = computed(() => moving.value && (!preview.value || unclaimable.value || (workspaceMode.value !== 'existing' && preview.value.exists && !preview.value.empty) || (workspaceMode.value === 'git' && !preview.value.git_configured) || (!!props.bot.relay_server_id && !preview.value.source_online && (!allowStoredMemory.value || !preview.value.memory_snapshot_at || workspaceMode.value === 'copy'))))
watch(targetDirectory, () => { preview.value = undefined })
async function checkTarget() {
  if (!targetId.value || !targetDirectory.value) return
  checking.value = true
  try { preview.value = await workspace.preview(props.bot.id, targetId.value, targetDirectory.value) } catch (e) { fail(e) } finally { checking.value = false }
}
onBeforeUnmount(() => clearInterval(progressTimer))
const models = ref<string[]>([])
const model = ref<string>('')
/** If-Match 用的版本号：版本冲突后刷新成最新值。 */
const version = ref(props.bot.version)

const target = computed(() => relayList.value.find((r) => r.id === targetId.value) ?? null)
// 挂载时会预选当前 relay，此时直接确认就是空操作：后端照样写一条 diff 为空的 bot.switch_relay 审计。
const unchanged = computed(() => targetId.value === props.bot.relay_server_id && model.value === props.bot.model && targetDirectory.value === props.bot.working_dir)
// 跨 backend（claude ↔ codex）要红字强提示：会话上下文与工具链都不一样。
const backendChanged = computed(() => !!target.value && !!model.value && backendOf(model.value) !== props.bot.backend)

function fail(e: unknown): void {
  ElMessage.error(errorMessage(e))
}

/** 只有乐观锁冲突才提示并发修改，并拉最新版本号；其它 409 给后端原话。 */
async function reloadVersion(): Promise<void> {
  ElMessage.warning(t('common.conflictReloaded'))
  try {
    version.value = (await bots.get(props.bot.id)).version
  } catch (e) {
    fail(e)
  }
}

/** 选中一台 relay：载入它的有效模型集，当前模型还在集合里就留着，否则落到该 relay 的默认模型。 */
async function pick(id: string): Promise<void> {
  targetId.value = id
  preview.value = undefined
  allowStoredMemory.value = false
  targetDirectory.value = id === props.bot.relay_server_id ? props.bot.working_dir : `${relayList.value.find(r => r.id === id)?.workspace_root || '/home/ai'}/${props.bot.bot_key || 'project'}`
  try {
    const info = await relays.models(id)
    models.value = info.models
    model.value = info.models.includes(props.bot.model) ? props.bot.model : (info.default ?? info.models[0] ?? '')
  } catch (e) {
    models.value = []
    model.value = ''
    fail(e)
  }
}

async function confirm(): Promise<void> {
  if (!targetId.value || !model.value || unchanged.value || targetBlocked.value) return
  saving.value = true
  progressTimer = setInterval(() => { void workspace.get(props.bot.id).then(s => { progress.value = s.phase || t('workspaceFiles.states.' + s.state, s.state) }).catch(() => {}) }, 3000)
  try {
    const result = await bots.switchRelay(props.bot.id, { relay_server_id: targetId.value, model: model.value, ...(moving.value ? { workspace_mode: workspaceMode.value, target_directory: targetDirectory.value, allow_stored_memory: allowStoredMemory.value } : {}) }, version.value)
    ElMessage.success(t('bots.switch.done'))
    emit('switched', result)
    emit('update:visible', false)
  } catch (e) {
    if (isVersionConflict(e)) await reloadVersion()
    else fail(e)
  } finally {
    saving.value = false
    clearInterval(progressTimer)
  }
}

onMounted(async () => {
  loading.value = true
  try {
    relayList.value = (await relays.list({ is_active: true, per_page: 200 })).items
  } catch (e) {
    fail(e)
  } finally {
    loading.value = false
  }
  // 默认选中当前这台：换到自己是允许的（后端不校验团队），这个对话框同时也是「只换模型」的入口。
  const cur = props.bot.relay_server_id
  if (cur && relayList.value.some((r) => r.id === cur)) await pick(cur)
})

defineExpose({ pick, confirm, checkTarget })
</script>

<template>
  <el-dialog
    :close-on-click-modal="false"
    :model-value="visible"
    :title="t('bots.switch.title')"
    width="900px"
    class="workspace-switch-dialog"
    :close-on-press-escape="!saving"
    :show-close="!saving"
    @update:model-value="emit('update:visible', $event)"
  >
    <el-table
      v-loading="loading"
      :data="relayList"
      max-height="360"
    >
      <el-table-column width="56">
        <template #default="{ row }: { row: RelayOut }">
          <el-radio
            :model-value="targetId"
            :disabled="saving"
            :value="row.id"
            :data-test="'pick-' + row.id"
            @change="pick(row.id)"
          >
            <span />
          </el-radio>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('relays.name')"
      >
        <template #default="{ row }: { row: RelayOut }">
          <div>{{ row.name }}</div>
          <el-tag
            v-if="row.id === bot.relay_server_id"
            size="small"
            type="info"
          >
            {{ t('bots.switch.current') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.provider')"
        width="150"
      >
        <template #default="{ row }: { row: RelayOut }">
          <div>{{ row.model_provider }}</div>
          <!-- 后端标签按默认模型推断（后端也是按模型名定 backend 的）；没有默认模型就不猜。 -->
          <el-tag
            v-if="row.default_model"
            size="small"
            type="info"
          >
            {{ t(`bots.backend.${backendOf(row.default_model)}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('runtimeNodes.nodes')"
        width="160"
      >
        <template #default="{ row }: { row: RelayOut }">
          {{ row.runtime_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.rate5h')"
        width="120"
      >
        <template #default="{ row }: { row: RelayOut }">
          <el-progress
            v-if="pctNum(row.rate_limit_5h_used_pct) !== null"
            :percentage="pctNum(row.rate_limit_5h_used_pct) ?? 0"
            :color="pctColor(row.rate_limit_5h_used_pct)"
          />
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.rate7d')"
        width="120"
      >
        <template #default="{ row }: { row: RelayOut }">
          <el-progress
            v-if="pctNum(row.rate_limit_7d_used_pct) !== null"
            :percentage="pctNum(row.rate_limit_7d_used_pct) ?? 0"
            :color="pctColor(row.rate_limit_7d_used_pct)"
          />
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.healthTitle')"
        width="90"
      >
        <template #default="{ row }: { row: RelayOut }">
          <HealthTag :status="row.health_status" />
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.team')"
        width="120"
      >
        <template #default="{ row }: { row: RelayOut }">
          {{ row.team_name ?? t('relays.publicPool') }}
        </template>
      </el-table-column>
    </el-table>

    <el-form
      class="switch-form"
      label-width="120px"
      @submit.prevent
    >
      <el-form-item :label="t('bots.switch.target')">
        <span data-test="switch-target">{{ target ? target.name : '—' }}</span>
      </el-form-item>
      <el-form-item
        :label="t('bots.switch.model')"
        data-test="switch-model"
      >
        <el-select
          v-model="model"
          filterable
          :disabled="!target || saving"
          style="width: 320px"
        >
          <el-option
            v-for="m in models"
            :key="m"
            :label="m"
            :value="m"
          />
        </el-select>
      </el-form-item>
      <template v-if="target">
        <el-form-item :label="t('workspaceFiles.source')">
          <code>{{ bot.working_dir }}</code>
        </el-form-item>
        <el-form-item :label="t('workspaceFiles.target')">
          <el-input
            v-model="targetDirectory"
            :disabled="saving"
          />
        </el-form-item>
        <el-form-item :label="t('workspaceFiles.mode')">
          <el-select
            v-model="workspaceMode"
            :disabled="saving"
          >
            <el-option
              value="copy"
              :label="t('workspaceFiles.copy')"
            /><el-option
              value="git"
              :label="t('workspaceFiles.restore')"
            /><el-option
              value="existing"
              :label="t('workspaceFiles.existing')"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button
            :loading="checking"
            :disabled="saving || !targetDirectory"
            data-test="check-workspace"
            @click="checkTarget"
          >
            {{ t('workspaceFiles.check') }}
          </el-button>
        </el-form-item>
        <template v-if="preview">
          <el-alert
            :title="t('workspaceFiles.checked')"
            type="success"
            :closable="false"
          />
          <el-alert
            v-if="preview.exists && !preview.empty && workspaceMode !== 'existing'"
            :title="t('workspaceFiles.occupied')"
            type="warning"
            :closable="false"
          />
          <el-alert
            v-if="unclaimable"
            :title="t('workspaceFiles.unclaimable')"
            type="warning"
            :closable="false"
            data-test="workspace-unclaimable"
          />
          <el-alert
            v-if="takeover"
            :title="t('workspaceFiles.takeover')"
            type="info"
            :closable="false"
            data-test="workspace-takeover"
          />
          <template v-if="bot.relay_server_id && !preview.source_online">
            <el-alert
              :title="t('workspaceFiles.offline')"
              type="warning"
              :closable="false"
            /><p>{{ t('workspaceFiles.snapshot') }}: {{ preview.memory_snapshot_at || '—' }}</p><el-checkbox
              v-model="allowStoredMemory"
              :disabled="!preview.memory_snapshot_at || saving"
            >
              {{ t('workspaceFiles.consent') }}
            </el-checkbox>
          </template>
        </template>
      </template>
    </el-form>
    <el-alert
      v-if="saving"
      :title="progress || t('workspaceFiles.progress')"
      type="info"
      :closable="false"
    />

    <el-alert
      :title="t('bots.switch.warnings')"
      type="warning"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="backendChanged"
      class="backend-change"
      :title="t('bots.switch.backendChange')"
      type="error"
      :closable="false"
      show-icon
    />

    <template #footer>
      <el-button
        data-test="switch-cancel"
        :disabled="saving"
        @click="emit('update:visible', false)"
      >
        {{ t('common.cancel') }}
      </el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="!target || !model || unchanged || targetBlocked"
        data-test="switch-confirm"
        @click="confirm"
      >
        {{ t('bots.switch.confirm') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.switch-form { margin-top: 12px; }
.backend-change { margin-top: 8px; }
</style>

<style>
.workspace-switch-dialog {max-width:calc(100vw - 24px);display:flex;flex-direction:column;max-height:90vh;margin-top:5vh!important}.workspace-switch-dialog .el-dialog__body{overflow:auto;min-height:0}.workspace-switch-dialog .el-dialog__footer{flex-shrink:0}.workspace-switch-dialog code{overflow-wrap:anywhere}.workspace-switch-dialog .el-select{max-width:100%}
</style>
