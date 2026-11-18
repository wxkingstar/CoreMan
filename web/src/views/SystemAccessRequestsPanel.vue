<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { systemAccess, type SystemAccessRequest } from '@/api/infrastructure'
import { formatDateTime } from '@/utils/format'

// active：所在的 tab 是否正在显示；显示时才加载，切回来刷新一次，不做定时刷新。
const props = withDefaults(defineProps<{ active?: boolean }>(), { active: true })
const { t } = useI18n()
const PER_PAGE = 50

const rows = ref<SystemAccessRequest[]>([])
const status = ref<'pending' | 'all'>('pending')
// 页码只放在组件里：外层「业务系统」列表已经把 page 写进地址栏，这里再写会互相覆盖。
const page = ref(1), total = ref(0)
const loading = ref(false), error = ref('')
const deciding = ref<string | null>(null)
let generation = 0

async function load() {
  const request = ++generation
  loading.value = true; error.value = ''
  try {
    const data = await systemAccess.list(status.value, page.value)
    if (request !== generation) return
    rows.value = data.items; total.value = data.total
  } catch (e) {
    if (request !== generation) return
    error.value = errorMessage(e); ElMessage.error(error.value)
  } finally {
    if (request === generation) loading.value = false
  }
}

function switchStatus() { page.value = 1; void load() }
watch(() => props.active, active => { if (active) void load() })
onMounted(() => { if (props.active) void load() })

const statusTag = { pending: 'warning', approved: 'success', rejected: 'danger', withdrawn: 'info' } as const

async function decide(row: SystemAccessRequest, decision: 'approve' | 'reject') {
  if (deciding.value) return
  let note = ''
  try {
    if (decision === 'approve') {
      await ElMessageBox.confirm(t('systemAccess.approveConfirm', { name: row.bot_name }), t('systemAccess.approve'), {
        type: 'warning',
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel'),
      })
    } else {
      const answer = await ElMessageBox.prompt(t('systemAccess.rejectPrompt'), t('systemAccess.reject'), {
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel'),
        inputValidator: (value: string) => Boolean(value?.trim()) || t('systemAccess.rejectRequired'),
      })
      note = answer.value.trim()
    }
  } catch {
    return
  }
  deciding.value = row.id
  try {
    await systemAccess.decide(row.id, decision, note)
    ElMessage.success(t(decision === 'approve' ? 'systemAccess.approved' : 'systemAccess.rejected'))
  } catch (e) {
    // 409：已经处理过、或申请已失效被自动撤回；重取一次让列表对上。
    ElMessage.error(errorMessage(e))
  } finally {
    deciding.value = null
  }
  await load()
}
</script>

<template>
  <div class="access-panel">
    <div class="panel-header">
      <el-radio-group
        v-model="status"
        size="small"
        data-test="access-filter"
        @change="switchStatus"
      >
        <el-radio-button value="pending">
          {{ t('systemAccess.onlyPending') }}
        </el-radio-button>
        <el-radio-button value="all">
          {{ t('systemAccess.all') }}
        </el-radio-button>
      </el-radio-group>
      <el-button
        data-test="refresh-access"
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
      :empty-text="t('systemAccess.empty')"
    >
      <el-table-column
        width="170"
        :label="t('systemAccess.createdAt')"
      >
        <template #default="{ row }: { row: SystemAccessRequest }">
          {{ formatDateTime(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="120"
        prop="bot_name"
        :label="t('systemAccess.bot')"
      />
      <el-table-column
        min-width="140"
        :label="t('systemAccess.system')"
      >
        <template #default="{ row }: { row: SystemAccessRequest }">
          {{ row.system_name }}（{{ row.system_key }}）
        </template>
      </el-table-column>
      <el-table-column
        width="100"
        :label="t('systemAccess.level')"
      >
        <template #default="{ row }: { row: SystemAccessRequest }">
          {{ row.write ? t('systemAccess.write') : t('systemAccess.read') }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="220"
        :label="t('systemAccess.reason')"
      >
        <template #default="{ row }: { row: SystemAccessRequest }">
          <div>{{ row.reason }}</div>
          <div
            v-if="row.decision_note"
            class="note"
          >
            {{ row.decision_note }}
          </div>
        </template>
      </el-table-column>
      <el-table-column
        min-width="100"
        prop="requester"
        :label="t('systemAccess.requester')"
      />
      <el-table-column
        width="100"
        :label="t('systemAccess.status')"
      >
        <template #default="{ row }: { row: SystemAccessRequest }">
          <el-tag :type="statusTag[row.status]">
            {{ t(`systemAccess.statuses.${row.status}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        width="140"
        :label="t('common.actions')"
      >
        <template #default="{ row }: { row: SystemAccessRequest }">
          <template v-if="row.status === 'pending'">
            <el-button
              link
              type="primary"
              :data-test="'approve-' + row.id"
              :loading="deciding === row.id"
              @click="decide(row, 'approve')"
            >
              {{ t('systemAccess.approve') }}
            </el-button><el-button
              link
              type="danger"
              :data-test="'reject-' + row.id"
              :disabled="deciding === row.id"
              @click="decide(row, 'reject')"
            >
              {{ t('systemAccess.reject') }}
            </el-button>
          </template>
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
.panel-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.note { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 2px; }
</style>
