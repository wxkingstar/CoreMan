<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { systemBatches, type SystemBatch, type SystemBatchItem, type SystemBatchItemFilter } from '@/api/infrastructure'
import { formatDateTime } from '@/utils/format'

// 一个批次每一项的写入参数与结果摘要；按页取、按状态筛，打开时取第一页。
const props = defineProps<{ batch: SystemBatch | null }>()
const visible = defineModel<boolean>({ required: true })
const { t } = useI18n()
const PER_PAGE = 50
const FILTERS: SystemBatchItemFilter[] = ['ok', 'failed', 'unknown', 'skipped', 'pending']
const statusTag: Record<string, 'success' | 'danger' | 'warning' | 'info' | 'primary'> = {
  ok: 'success', http_error: 'danger', unreachable: 'danger', unknown: 'warning', sending: 'primary', skipped: 'info', pending: 'info',
}

const rows = ref<SystemBatchItem[]>([])
const page = ref(1), total = ref(0)
const filter = ref<SystemBatchItemFilter | ''>('')
const loading = ref(false), error = ref('')
let generation = 0

async function load() {
  if (!props.batch) return
  const request = ++generation
  loading.value = true; error.value = ''
  try {
    const data = await systemBatches.items(props.batch.id, page.value, PER_PAGE, filter.value || undefined)
    if (request !== generation) return
    rows.value = data.items; total.value = data.total
  } catch (e) {
    if (request === generation) error.value = errorMessage(e)
  } finally {
    if (request === generation) loading.value = false
  }
}

function reset() { page.value = 1; void load() }
watch(() => [visible.value, props.batch?.id] as const, ([open]) => {
  if (!open) return
  filter.value = ''; rows.value = []; total.value = 0
  reset()
})

// 参数原样展示：空的部分不显示，嵌套对象保留结构。
function sent(item: SystemBatchItem): string {
  const parts: Record<string, unknown> = {}
  if (item.path_params) parts.path_params = item.path_params
  if (item.query) parts.query = item.query
  if (item.body !== undefined && item.body !== null) parts.body = item.body
  return Object.keys(parts).length ? JSON.stringify(parts, null, 2) : '—'
}
const before = (item: SystemBatchItem) => item.before ? JSON.stringify(item.before, null, 2) : '—'
</script>

<template>
  <el-drawer
    v-model="visible"
    size="80%"
    :title="batch ? `${batch.system_name || batch.system || '—'} · ${batch.operation_summary || batch.operation_id || '—'}` : ''"
    data-test="batch-items-drawer"
  >
    <div class="items-toolbar">
      <el-radio-group
        v-model="filter"
        size="small"
        data-test="batch-items-filter"
        @change="reset"
      >
        <el-radio-button value="">
          {{ t('systemBatches.allItems') }}
        </el-radio-button>
        <el-radio-button
          v-for="key in FILTERS"
          :key="key"
          :value="key"
        >
          {{ t(`systemBatches.itemFilter.${key}`) }}
        </el-radio-button>
      </el-radio-group>
      <el-button
        size="small"
        data-test="refresh-batch-items"
        @click="load"
      >
        {{ t('common.refresh') }}
      </el-button>
    </div>
    <LoadState
      :error="error"
      @retry="load"
    />
    <el-table
      v-if="!error"
      v-loading="loading"
      :data="rows"
      :empty-text="t('systemBatches.noItems')"
      data-test="batch-items-table"
    >
      <el-table-column
        width="70"
        prop="seq"
        :label="t('systemBatches.seq')"
      />
      <el-table-column
        min-width="140"
        :label="t('systemBatches.target')"
      >
        <template #default="{ row }: { row: SystemBatchItem }">
          {{ row.target || '—' }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="260"
        :label="t('systemBatches.sent')"
      >
        <template #default="{ row }: { row: SystemBatchItem }">
          <pre class="json">{{ sent(row) }}</pre>
        </template>
      </el-table-column>
      <el-table-column
        min-width="160"
        :label="t('systemBatches.before')"
      >
        <template #default="{ row }: { row: SystemBatchItem }">
          <pre class="json">{{ before(row) }}</pre>
        </template>
      </el-table-column>
      <el-table-column
        width="130"
        :label="t('common.status')"
      >
        <template #default="{ row }: { row: SystemBatchItem }">
          <el-tag
            size="small"
            :type="statusTag[row.status] ?? 'info'"
            :data-test="'item-status-' + row.seq"
          >
            {{ t(`systemBatches.itemStatus.${row.status}`) }}{{ row.status_code ? ` · ${row.status_code}` : '' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        min-width="220"
        :label="t('systemBatches.brief')"
      >
        <template #default="{ row }: { row: SystemBatchItem }">
          <span class="brief">{{ row.brief || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column
        width="170"
        :label="t('systemBatches.finishedAt')"
      >
        <template #default="{ row }: { row: SystemBatchItem }">
          {{ row.finished_at ? formatDateTime(row.finished_at) : '—' }}
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="page"
      :total="total"
      :page-size="PER_PAGE"
      layout="total, prev, pager, next"
      hide-on-single-page
      @current-change="load"
    />
  </el-drawer>
</template>

<style scoped>
.items-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.json { margin: 0; max-height: 160px; overflow: auto; font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-all; }
.brief { font-size: 12px; word-break: break-all; }
</style>
