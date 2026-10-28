<script setup lang="ts">
import { errorMessage, isVersionConflict } from '@/utils/errors'
import { ElMessage } from 'element-plus'
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { workspace, type SwitchPreview } from '@/api/workspace'
import { bots, relays } from '@/api/admin'

import type { BotOut, RelayOut, SwitchRelayOut } from '@/api/types'
import HealthTag from '@/components/HealthTag.vue'
import QuotaBar from '@/components/QuotaBar.vue'
import { pctNum } from '@/utils/format'
import { backendOf } from '@/utils/models'

const props = defineProps<{ bot: BotOut; visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [boolean]; switched: [SwitchRelayOut] }>()

const { t } = useI18n()

const relayList = ref<RelayOut[]>([])
const loading = ref(false)
const saving = ref(false)
const targetId = ref<string | null>(null)
const workspaceMode = ref<'copy' | 'existing'>('copy')
const targetDirectory = ref('')
const allowStoredMemory = ref(false)
const preview = ref<SwitchPreview>()
const checking = ref(false)
/** 换机经过的每一步：后端每进一步就更新 workspace_phase，这里轮询记下来，带上起始时间。 */
const steps = ref<{ phase: string; at: number }[]>([])
const now = ref(Date.now())
let progressTimer: ReturnType<typeof setInterval> | undefined
let checkTimer: ReturnType<typeof setTimeout> | undefined
let checkSeq = 0
const moving = computed(() => !!targetId.value && (targetId.value !== props.bot.relay_server_id || targetDirectory.value !== props.bot.working_dir))
// 使用已有目录：本员工的，或没有任何员工认领（如另一套机器人系统在用的目录，直接接管）。
// 复制当前文件会整体覆盖目标目录，但不能覆盖其他员工的目录。
const unclaimable = computed(() => !!preview.value && (workspaceMode.value === 'existing' ? !preview.value.exists || (!preview.value.owned && preview.value.marked) : preview.value.exists && !preview.value.empty && !preview.value.owned && preview.value.marked))
const takeover = computed(() => !!preview.value && workspaceMode.value === 'existing' && preview.value.exists && !preview.value.owned && !preview.value.marked)
const targetBlocked = computed(() => moving.value && (!preview.value || unclaimable.value || (workspaceMode.value !== 'existing' && preview.value.exists && !preview.value.empty && !props.bot.relay_server_id) || (!!props.bot.relay_server_id && !preview.value.source_online && (!allowStoredMemory.value || !preview.value.memory_snapshot_at || workspaceMode.value === 'copy'))))
// 选中目标或改了目录就自动检查（输入时防抖），不用再点按钮；共享盘直接判出来，处理方式不用选。
watch(targetDirectory, () => {
  preview.value = undefined
  clearTimeout(checkTimer)
  checkTimer = setTimeout(() => { void checkTarget() }, 600)
})
async function checkTarget() {
  clearTimeout(checkTimer)
  if (!moving.value || !targetId.value || !targetDirectory.value) return
  const seq = ++checkSeq
  checking.value = true
  try {
    const result = await workspace.preview(props.bot.id, targetId.value, targetDirectory.value)
    if (seq === checkSeq) preview.value = result
  } catch (e) { if (seq === checkSeq) fail(e) } finally { if (seq === checkSeq) checking.value = false }
}
function track(phase: string | null | undefined): void {
  now.value = Date.now()
  if (phase && steps.value[steps.value.length - 1]?.phase !== phase) steps.value.push({ phase, at: now.value })
}
const seconds = (from: number, to: number) => Math.max(0, Math.round((to - from) / 1000))
onBeforeUnmount(() => { clearInterval(progressTimer); clearTimeout(checkTimer) })
const models = ref<string[]>([])
const model = ref<string>('')
/** If-Match 用的版本号：版本冲突后刷新成最新值。 */
const version = ref(props.bot.version)

const target = computed(() => relayList.value.find((r) => r.id === targetId.value) ?? null)
// 挂载时会预选当前 relay，此时直接确认就是空操作：后端照样写一条 diff 为空的 bot.switch_relay 审计。
const unchanged = computed(() => targetId.value === props.bot.relay_server_id && model.value === props.bot.model && targetDirectory.value === props.bot.working_dir)
// 跨 backend（claude ↔ codex）要红字强提示：会话上下文与工具链都不一样。
const backendChanged = computed(() => !!target.value && !!model.value && backendOf(model.value) !== props.bot.backend)
// 未安装或未登录的后端采不到额度，库里留着的是旧值，不展示。
const hasQuota = (row: RelayOut) => !['not_installed', 'login_required'].includes(row.unavailable_reason ?? '')

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

/** 目标运行时上的默认目录，与后端 target_path 同一规则：保留当前目录相对项目主目录的路径，
 * 共享盘上就是同一个目录；不在主目录下时才落到 bot_key。 */
function defaultDirectory(id: string): string {
  const root = relayList.value.find(r => r.id === id)?.workspace_root || '/home/ai'
  const current = relayList.value.find(r => r.id === props.bot.relay_server_id)?.workspace_root
  const prefix = current ? current.replace(/\/+$/, '') + '/' : ''
  const relative = prefix && props.bot.working_dir?.startsWith(prefix)
    ? props.bot.working_dir.slice(prefix.length)
    : props.bot.bot_key || 'project'
  return `${root.replace(/\/+$/, '')}/${relative}`
}

/** 选中一台 relay：载入它的有效模型集，当前模型还在集合里就留着，否则落到该 relay 的默认模型。 */
async function pick(id: string): Promise<void> {
  if (id !== props.bot.relay_server_id && relayList.value.find(r => r.id === id)?.unavailable_reason !== null) return
  targetId.value = id
  preview.value = undefined
  allowStoredMemory.value = false
  targetDirectory.value = id === props.bot.relay_server_id ? props.bot.working_dir : defaultDirectory(id)
  void checkTarget()
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
  if (!targetId.value || !model.value || unchanged.value || targetBlocked.value || (targetId.value !== props.bot.relay_server_id && target.value?.unavailable_reason !== null)) return
  saving.value = true
  steps.value = [{ phase: t('workspaceFiles.starting'), at: Date.now() }]
  progressTimer = setInterval(() => {
    now.value = Date.now()
    void workspace.get(props.bot.id).then(s => { if (s.state === 'migrating') track(s.phase) }).catch(() => {})
  }, 1000)
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
    width="1200px"
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
            :disabled="saving || (row.id !== bot.relay_server_id && row.unavailable_reason !== null)"
            :value="row.id"
            :data-test="'pick-' + row.id"
            @change="pick(row.id)"
          >
            <span />
          </el-radio>
        </template>
      </el-table-column>
      <el-table-column
        min-width="200"
        :label="t('relays.name')"
      >
        <template #default="{ row }: { row: RelayOut }">
          <div>{{ row.name }}</div>
          <p
            v-if="row.unavailable_reason !== null"
            class="muted"
          >
            {{ t('runtimeNodes.unavailable.' + (row.unavailable_reason ?? 'unknown')) }}
          </p>
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
        width="170"
      >
        <template #default="{ row }: { row: RelayOut }">
          <QuotaBar
            v-if="hasQuota(row) && pctNum(row.rate_limit_5h_used_pct) !== null"
            :pct="row.rate_limit_5h_used_pct"
            :resets-at="row.rate_limit_5h_resets_at"
          />
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.rate7d')"
        width="170"
      >
        <template #default="{ row }: { row: RelayOut }">
          <QuotaBar
            v-if="hasQuota(row) && pctNum(row.rate_limit_7d_used_pct) !== null"
            :pct="row.rate_limit_7d_used_pct"
            :resets-at="row.rate_limit_7d_resets_at"
          />
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('relays.healthTitle')"
        width="100"
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
        <el-form-item
          v-if="!preview?.shared"
          :label="t('workspaceFiles.mode')"
        >
          <el-select
            v-model="workspaceMode"
            :disabled="saving"
          >
            <el-option
              value="copy"
              :label="t('workspaceFiles.copy')"
            /><el-option
              value="existing"
              :label="t('workspaceFiles.existing')"
            />
          </el-select>
        </el-form-item>
        <p
          v-if="moving && checking"
          class="muted"
          data-test="checking-workspace"
        >
          {{ t('workspaceFiles.checking') }}
        </p>
        <template v-if="preview">
          <el-alert
            v-if="preview.shared"
            :title="t('workspaceFiles.shared')"
            type="info"
            :closable="false"
            data-test="workspace-shared"
          />
          <el-alert
            v-if="preview.exists && !preview.empty && workspaceMode !== 'existing' && !unclaimable && !preview.shared"
            :title="bot.relay_server_id ? t('workspaceFiles.replace') : t('workspaceFiles.occupied')"
            type="warning"
            :closable="false"
            data-test="workspace-replace"
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
    <div
      v-if="saving"
      class="switch-steps"
      data-test="switch-steps"
    >
      <p class="muted">
        {{ t('workspaceFiles.progress') }}
      </p>
      <ol>
        <li
          v-for="(step, i) in steps"
          :key="i"
          :class="{ current: i === steps.length - 1 }"
        >
          <span>{{ i === steps.length - 1 ? '…' : '✓' }}</span>
          {{ step.phase }}
          <span class="muted">{{ seconds(step.at, steps[i + 1]?.at ?? now) }}s</span>
        </li>
      </ol>
    </div>

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
        :disabled="!target || !model || unchanged || targetBlocked || (targetId !== bot.relay_server_id && target.unavailable_reason !== null)"
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
.switch-steps { margin: 8px 0; }
.switch-steps ol { list-style: none; padding: 0; margin: 4px 0 0; }
.switch-steps li { padding: 2px 0; }
.switch-steps li.current { font-weight: 600; }
</style>

<style>
.workspace-switch-dialog {max-width:calc(100vw - 24px);display:flex;flex-direction:column;max-height:90vh;margin-top:5vh!important}.workspace-switch-dialog .el-dialog__body{overflow:auto;min-height:0}.workspace-switch-dialog .el-dialog__footer{flex-shrink:0}.workspace-switch-dialog code{overflow-wrap:anywhere}.workspace-switch-dialog .el-select{max-width:100%}
</style>
