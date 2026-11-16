<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { systemBatches, type SystemBatch, type SystemBatchPhase } from '@/api/infrastructure'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const PER_PAGE = 20
const REFRESH_MS = 5000
// 这几种状态还会自己往前走，列表要跟着刷新；其余都是终态。
const LIVE: SystemBatchPhase[] = ['awaiting', 'running', 'paused']
const RESULTS = ['ok', 'failed', 'unknown', 'skipped'] as const

const rows = ref<SystemBatch[]>([])
// 页码只放在组件里：外层「业务系统」列表已经把 page 写进地址栏，这里再写会互相覆盖。
const page = ref(1), total = ref(0)
const loading = ref(false), error = ref('')
const stopping = ref<string | null>(null)
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
let alive = true

function schedule() {
  clearTimeout(timer)
  if (alive && rows.value.some(row => LIVE.includes(row.phase))) timer = setTimeout(() => { void load(true) }, REFRESH_MS)
}

// quiet：定时刷新不盖加载遮罩，失败也不弹提示（保留上一次的列表，下一轮再试）。
async function load(quiet = false) {
  const request = ++generation
  clearTimeout(timer)
  if (!quiet) { loading.value = true; error.value = '' }
  try {
    const data = await systemBatches.list(page.value, PER_PAGE)
    if (request !== generation) return
    rows.value = data.items; total.value = data.total; error.value = ''
  } catch (e) {
    if (request !== generation || quiet) return
    error.value = errorMessage(e); ElMessage.error(error.value)
  } finally {
    if (request === generation) { loading.value = false; schedule() }
  }
}

const done = (row: SystemBatch) => Math.max(0, row.count - row.pending)
const percent = (row: SystemBatch) => row.count > 0 ? Math.min(100, Math.round(done(row) / row.count * 100)) : 0
const canStop = (row: SystemBatch) => (row.phase === 'running' || row.phase === 'paused') && !row.stopping
const phaseTag: Partial<Record<SystemBatchPhase, 'primary' | 'warning' | 'success'>> = { running: 'primary', paused: 'warning', finished: 'success' }
const statusTag = (row: SystemBatch) => row.stopping ? 'danger' : phaseTag[row.phase] ?? 'info'
function statusText(row: SystemBatch) {
  if (row.stopping) return t('systemBatches.stopping')
  const label = t(`systemBatches.phase.${row.phase}`)
  return row.phase === 'paused' && row.pause_reason ? `${label} · ${t(`systemBatches.pauseReason.${row.pause_reason}`)}` : label
}
const resultTag = { ok: 'success', failed: 'danger', unknown: 'warning', skipped: 'info' } as const

async function stop(row: SystemBatch) {
  if (stopping.value) return
  try {
    await ElMessageBox.confirm(t('systemBatches.stopConfirm', { n: row.pending }), t('systemBatches.stop'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  stopping.value = row.id
  try {
    // 确认框开着的时候列表可能已经被定时刷新换过一轮，按 id 换掉当前列表里的那一行。
    const updated = await systemBatches.stop(row.id)
    // 停止前发出的列表请求带回的是旧状态，作废它，免得「停止」按钮又亮回来。
    generation++; loading.value = false
    rows.value = rows.value.map(item => item.id === updated.id ? updated : item)
    ElMessage.success(t('systemBatches.stopRequested'))
    schedule()
  } catch (e) {
    // 409：批次刚好自己结束了，或别人已经点过停止，重取一次让列表对上。
    ElMessage.error(errorMessage(e))
    await load(true)
  } finally {
    stopping.value = null
  }
}

onMounted(() => { void load() })
onBeforeUnmount(() => { alive = false; clearTimeout(timer) })
</script>

<template>
  <div class="batches-panel">
    <div class="panel-header">
      <h3>{{ t('systemBatches.title') }}</h3>
      <el-button
        data-test="refresh-batches"
        @click="load()"
      >
        {{ t('common.refresh') }}
      </el-button>
    </div>
    <LoadState
      :error="error"
      @retry="load()"
    />
    <el-table
      v-if="!error"
      v-loading="loading"
      :data="rows"
      :empty-text="t('systemBatches.empty')"
    >
      <el-table-column
        width="170"
        :label="t('systemBatches.createdAt')"
      >
        <template #default="{ row }: { row: SystemBatch }">
          {{ formatDateTime(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="120"
        :label="t('systemBatches.bot')"
      >
        <template #default="{ row }: { row: SystemBatch }">
          {{ row.bot_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="100"
        :label="t('systemBatches.requester')"
      >
        <template #default="{ row }: { row: SystemBatch }">
          {{ row.user_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="200"
        :label="t('systemBatches.operation')"
      >
        <template #default="{ row }: { row: SystemBatch }">
          {{ row.system_name || row.system || '—' }} · {{ row.operation_summary || row.operation_id || '—' }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="150"
        :label="t('common.status')"
      >
        <template #default="{ row }: { row: SystemBatch }">
          <el-tag
            :data-test="'batch-status-' + row.id"
            :type="statusTag(row)"
          >
            {{ statusText(row) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        min-width="170"
        :label="t('systemBatches.progress')"
      >
        <template #default="{ row }: { row: SystemBatch }">
          <div :data-test="'batch-progress-' + row.id">
            {{ done(row) }} / {{ row.count }}
            <el-progress
              :percentage="percent(row)"
              :show-text="false"
              :stroke-width="6"
            />
          </div>
        </template>
      </el-table-column>
      <el-table-column
        min-width="220"
        :label="t('systemBatches.result')"
      >
        <template #default="{ row }: { row: SystemBatch }">
          <div
            class="results"
            :data-test="'batch-result-' + row.id"
          >
            <template
              v-for="key in RESULTS"
              :key="key"
            >
              <el-tag
                v-if="row[key] > 0"
                size="small"
                :type="resultTag[key]"
              >
                {{ t(`systemBatches.results.${key}`) }} {{ row[key] }}
              </el-tag>
            </template>
          </div>
        </template>
      </el-table-column>
      <el-table-column
        width="100"
        :label="t('common.actions')"
      >
        <template #default="{ row }: { row: SystemBatch }">
          <el-button
            link
            type="danger"
            :data-test="'stop-batch-' + row.id"
            :disabled="!canStop(row)"
            :loading="stopping === row.id"
            @click="stop(row)"
          >
            {{ t('systemBatches.stop') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="page"
      :total="total"
      :page-size="PER_PAGE"
      layout="prev, pager, next"
      hide-on-single-page
      @current-change="load()"
    />
  </div>
</template>

<style scoped>
.batches-panel { margin-top: 32px; }
.panel-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.results { display: flex; flex-wrap: wrap; gap: 4px; }
</style>
