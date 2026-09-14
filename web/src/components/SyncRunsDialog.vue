<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { platformApps } from '@/api/admin'
import type { SyncRun } from '@/api/types'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{ modelValue: boolean; appId: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const { t } = useI18n()

const runs = ref<SyncRun[]>([])
const loading = ref(false)

const STAT_FIELDS = ['departments', 'users_total', 'created', 'updated', 'disabled', 'reactivated'] as const
const CONFLICT_FIELDS = ['login_name_conflicts', 'field_conflicts'] as const

async function load() {
  if (!props.appId) {
    runs.value = []
    return
  }
  loading.value = true
  try {
    runs.value = await platformApps.runs(props.appId)
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.appId],
  ([visible]) => {
    if (visible) load()
  },
  { immediate: true },
)

function statusType(status: SyncRun['status']): 'primary' | 'success' | 'danger' | 'warning' {
  if (status === 'running') return 'primary'
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  return 'warning'
}

function statValue(run: SyncRun, key: string): number {
  const v = run.stats?.[key]
  return typeof v === 'number' ? v : 0
}

function conflictList(run: SyncRun, key: string): string[] {
  const v = run.stats?.[key]
  return Array.isArray(v) ? (v as string[]) : []
}

function conflictKeys(run: SyncRun): Array<(typeof CONFLICT_FIELDS)[number]> {
  return CONFLICT_FIELDS.filter((key) => conflictList(run, key).length > 0)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="t('apps.runs')"
    width="720px"
    destroy-on-close
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-table
      v-loading="loading"
      :data="runs"
      row-key="id"
    >
      <el-table-column
        prop="id"
        label="ID"
        width="60"
      />
      <el-table-column
        :label="t('apps.status')"
        width="90"
      >
        <template #default="{ row }: { row: SyncRun }">
          <el-tag :type="statusType(row.status)">
            {{ t('apps.runStatus.' + row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('apps.startedAt')"
        width="160"
      >
        <template #default="{ row }: { row: SyncRun }">
          {{ formatDateTime(row.started_at) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('apps.finishedAt')"
        width="160"
      >
        <template #default="{ row }: { row: SyncRun }">
          {{ formatDateTime(row.finished_at) }}
        </template>
      </el-table-column>
      <el-table-column width="200">
        <template #default="{ row }: { row: SyncRun }">
          <div
            v-for="key in STAT_FIELDS"
            :key="key"
            class="stat-line"
          >
            {{ t('apps.stats.' + key) }}: {{ statValue(row, key) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column min-width="200">
        <template #default="{ row }: { row: SyncRun }">
          <el-collapse v-if="conflictKeys(row).length > 0">
            <el-collapse-item
              v-for="key in conflictKeys(row)"
              :key="key"
              :title="`${t('apps.stats.' + key)} (${conflictList(row, key).length})`"
              :name="key"
            >
              <div
                v-for="c in conflictList(row, key)"
                :key="c"
              >
                {{ c }}
              </div>
            </el-collapse-item>
          </el-collapse>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('apps.error')"
      >
        <template #default="{ row }: { row: SyncRun }">
          <span :class="{ 'error-text': row.error }">{{ row.error ?? '-' }}</span>
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">
        {{ t('common.cancel') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.stat-line { line-height: 1.6; }
.error-text { color: var(--el-color-danger); }
</style>
