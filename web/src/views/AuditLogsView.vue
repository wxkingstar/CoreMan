<script setup lang="ts">
import LoadState from '@/components/LoadState.vue'
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { auditLogs } from '@/api/admin'
import type { AuditLogOut } from '@/api/types'
import { usePaged } from '@/composables/usePaged'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()

// 后端 action 前缀匹配（audit_logs.py: `action LIKE '<prefix>%'`）。这里列的是应用真正写入的
// 前缀，与 coreman/api/routers/*.py 里的 record_audit(action=...) 一一对应；下拉可自由输入，
// 想查具体动作（bot.switch_relay）直接敲即可。
const ACTION_PREFIXES = ['bot.', 'relay.', 'catalog.', 'platform_app.', 'user.', 'team.', 'settings.', 'auth.']
// 同样取自 record_audit(target_type=...)：这是精确匹配，拼错就查不到，所以给下拉。
const TARGET_TYPES = ['bot', 'relay_server', 'model_catalog', 'platform_app', 'user', 'team', 'settings']

type AuditFilters = {
  actor: string
  action: string
  target_type: string
  target_id: string
  since: string | null
  until: string | null
}

/** 空筛选项不进 query：`?actor=` 会被 FastAPI 当成空字符串（falsy 分支虽然放行，但没必要发）。 */
function clean(q: Record<string, unknown>): Record<string, string | number | boolean> {
  return Object.fromEntries(
    Object.entries(q).filter(([, v]) => v !== '' && v !== null && v !== undefined),
  ) as Record<string, string | number | boolean>
}

const paged = usePaged<AuditLogOut, AuditFilters>((q) => auditLogs.list(clean(q)), {
  actor: '',
  action: '',
  target_type: '',
  target_id: '',
  since: null,
  until: null,
})

// el-date-picker 的 v-model 是 Date[]，后端要 ISO 字符串，中间过一层。
const range = ref<[Date, Date] | null>(paged.filters.since && paged.filters.until ? [new Date(paged.filters.since), new Date(paged.filters.until)] : null)
watch(() => [paged.filters.since, paged.filters.until], ([since, until]) => {
  const next = since && until && Number.isFinite(Date.parse(since)) && Number.isFinite(Date.parse(until)) ? [new Date(since), new Date(until)] as [Date, Date] : null
  if (JSON.stringify(range.value) !== JSON.stringify(next)) range.value = next
})
watch(range, (v) => {
  paged.filters.since = v?.[0] ? v[0].toISOString() : null
  paged.filters.until = v?.[1] ? v[1].toISOString() : null
})

function hasDiff(diff: AuditLogOut['diff']): boolean {
  return !!diff && Object.keys(diff).length > 0
}

function actorOf(row: AuditLogOut): string {
  return row.actor_login || row.actor_id || '—'
}

function targetOf(row: AuditLogOut): string {
  if (!row.target_type && !row.target_id) return '—'
  return row.target_id ? `${row.target_type ?? '—'}/${row.target_id}` : (row.target_type ?? '—')
}

function search() {
  paged.page.value = 1
  paged.load()
}

function resetFilters() {
  paged.reset()
  range.value = null
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

onMounted(() => paged.load())

defineExpose({ paged, range })
</script>

<template>
  <div class="audit-view">
    <div class="page-header cm-page-header">
      <h2>{{ t('audit.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.audit') }}
      </p>
    </div>

    <el-form
      :inline="true"
      @submit.prevent
    >
      <el-form-item
        :label="t('audit.actor')"
        data-test="filter-actor"
      >
        <el-input
          v-model="paged.filters.actor"
          clearable
          style="width: 160px"
        />
      </el-form-item>
      <el-form-item
        :label="t('audit.action')"
        data-test="filter-action"
      >
        <el-select
          v-model="paged.filters.action"
          filterable
          allow-create
          clearable
          default-first-option
          :placeholder="t('audit.actionPrefixes')"
          style="width: 200px"
        >
          <el-option
            v-for="p in ACTION_PREFIXES"
            :key="p"
            :label="p"
            :value="p"
          />
        </el-select>
      </el-form-item>
      <el-form-item
        :label="t('audit.targetType')"
        data-test="filter-target-type"
      >
        <el-select
          v-model="paged.filters.target_type"
          filterable
          allow-create
          clearable
          default-first-option
          :placeholder="t('audit.targetType')"
          style="width: 170px"
        >
          <el-option
            v-for="tt in TARGET_TYPES"
            :key="tt"
            :label="tt"
            :value="tt"
          />
        </el-select>
      </el-form-item>
      <el-form-item
        :label="t('audit.targetId')"
        data-test="filter-target-id"
      >
        <el-input
          v-model="paged.filters.target_id"
          clearable
          style="width: 200px"
        />
      </el-form-item>
      <el-form-item
        :label="t('audit.timeRange')"
        data-test="filter-range"
      >
        <el-date-picker
          v-model="range"
          type="datetimerange"
          unlink-panels
          :start-placeholder="t('audit.since')"
          :end-placeholder="t('audit.until')"
        />
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          data-test="search"
          @click="search"
        >
          {{ t('common.search') }}
        </el-button>
        <el-button
          data-test="reset"
          @click="resetFilters"
        >
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
      row-key="id"
    >
      <el-table-column
        type="expand"
        :label="t('audit.diff')"
        width="80"
      >
        <template #default="{ row }: { row: AuditLogOut }">
          <pre
            v-if="hasDiff(row.diff)"
            class="diff"
          >{{ JSON.stringify(row.diff, null, 2) }}</pre>
          <span
            v-else
            class="muted"
          >{{ t('audit.noDiff') }}</span>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('audit.time')"
        width="180"
      >
        <template #default="{ row }: { row: AuditLogOut }">
          {{ formatDateTime(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('audit.actor')"
        width="160"
      >
        <template #default="{ row }: { row: AuditLogOut }">
          {{ actorOf(row) }}
        </template>
      </el-table-column>
      <el-table-column
        prop="action"
        :label="t('audit.action')"
        width="200"
      />
      <el-table-column
        min-width="140"
        :label="t('audit.target')"
      >
        <template #default="{ row }: { row: AuditLogOut }">
          {{ targetOf(row) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('audit.ip')"
        width="150"
      >
        <template #default="{ row }: { row: AuditLogOut }">
          {{ row.ip ?? '—' }}
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
  </div>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.diff {
  margin: 0;
  padding: 8px 12px;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}
</style>
