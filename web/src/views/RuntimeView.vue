<script setup lang="ts">
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { runtime } from '@/api/admin'
import type { DrainIn, RuntimeInstance, RuntimeLease, RuntimeOutboxItem, RuntimeQueue, RuntimeTask } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const auth = useAuthStore()
// 读端点对 team_lead / ai_committee / platform_admin 开放，写端点只对 platform_admin。
const canWrite = computed(() => auth.user?.role === 'platform_admin')

const REFRESH_MS = 10_000

const queue = ref<RuntimeQueue | null>(null)
const instances = ref<RuntimeInstance[]>([])
const leases = ref<RuntimeLease[]>([])
const tasks = ref<RuntimeTask[]>([])
const failedOutbox = ref<RuntimeOutboxItem[]>([])
const loading = ref(false), foregroundLoading = ref(false)
const refreshError = ref('')
let timer: ReturnType<typeof setInterval> | null = null

function fail(e: unknown) {
  ElMessage.error(e instanceof Error ? e.message : String(e))
}

const cards = computed(() => {
  const q = queue.value
  return [
    { key: 'queue', text: q ? `${q.queued.normal} / ${q.queued.fast}` : '—' },
    { key: 'running', text: q ? String(q.running) : '—' },
    { key: 'outbox', text: q ? `${q.outbox_pending} / ${q.outbox_failed}` : '—' },
    { key: 'streams', text: q ? String(q.streams_active) : '—' },
  ]
})

/** `silent` 给 10 秒自动刷新用：网络抖一下不要每 10 秒弹一次错误提示。 */
async function refresh(silent = false): Promise<void> {
  if (loading.value) return
  loading.value = true
  foregroundLoading.value = !silent
  try {
    const [q, ins, ls, ts, ob] = await Promise.all([
      runtime.queue(),
      runtime.instances(),
      runtime.leases(),
      runtime.tasks({ status: 'active', limit: 200 }),
      runtime.outbox({ status: 'failed', limit: 200 }),
    ])
    refreshError.value = ''
    queue.value = q
    instances.value = ins
    leases.value = ls
    tasks.value = ts
    failedOutbox.value = ob
  } catch (e) {
    refreshError.value = e instanceof Error ? e.message : String(e)
    if (!silent) fail(e)
  } finally {
    loading.value = false
    foregroundLoading.value = false
  }
}

async function confirmed(message: string, title: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
    return true
  } catch {
    return false
  }
}

/** 重试是幂等的重投，不加二次确认；排空与取消会打断在跑的东西，都要确认。 */
async function retry(item: RuntimeOutboxItem): Promise<void> {
  try {
    await runtime.retryOutbox(item.id)
    ElMessage.success(t('runtime.done'))
    await refresh()
  } catch (e) {
    fail(e)
  }
}

async function cancel(task: RuntimeTask): Promise<void> {
  if (!(await confirmed(t('runtime.cancelConfirm'), t('runtime.cancel')))) return
  try {
    await runtime.cancelTask(task.id)
    ElMessage.success(t('runtime.done'))
    await refresh()
  } catch (e) {
    fail(e)
  }
}

async function drain(body: DrainIn): Promise<void> {
  if (!(await confirmed(t('runtime.drainConfirm'), t('runtime.drain')))) return
  try {
    await runtime.drain(body)
    ElMessage.success(t('runtime.done'))
    await refresh()
  } catch (e) {
    fail(e)
  }
}

function instanceState(row: RuntimeInstance): { label: string; type: 'success' | 'info' | 'warning' } {
  if (row.stopped_at) return { label: t('runtime.stopped'), type: 'info' }
  if (row.drain_requested_at) return { label: t('runtime.draining'), type: 'warning' }
  return row.alive
    ? { label: t('runtime.alive'), type: 'success' }
    : { label: t('runtime.stopped'), type: 'warning' }
}

function leaseState(row: RuntimeLease): 'success' | 'info' | 'warning' | 'danger' {
  if (row.connection_state === 'subscribed') return 'success'
  if (row.connection_state === 'connecting') return 'warning'
  if (row.connection_state === 'auth_failed' || row.connection_state === 'kicked') return 'danger'
  return 'info'
}

function capacityOf(row: RuntimeInstance): string {
  return `${row.running} / ${row.capacity ?? '—'}`
}

onMounted(() => {
  refresh()
  timer = setInterval(() => void refresh(true), REFRESH_MS)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
  timer = null
})

defineExpose({ refresh })
</script>

<template>
  <div
    v-loading="foregroundLoading"
    class="runtime-view"
  >
    <div class="page-header cm-page-header">
      <h2>{{ t('runtime.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.runtime') }}
      </p>
      <el-button
        data-test="refresh"
        @click="refresh()"
      >
        {{ t('common.refresh') }}
      </el-button>
    </div>

    <LoadState
      :error="refreshError"
      @retry="refresh()"
    />
    <nav
      class="cm-section-nav"
      :aria-label="t('workspace.sections')"
    >
      <a href="#runtime-instances">{{ t('runtime.instances') }}</a><a href="#runtime-leases">{{ t('runtime.leases') }}</a><a href="#runtime-tasks">{{ t('runtime.tasks') }}</a><a href="#runtime-failedOutbox">{{ t('runtime.failedOutbox') }}</a>
    </nav>
    <el-row :gutter="16">
      <el-col
        v-for="c in cards"
        :key="c.key"
        :xs="12"
        :sm="6"
      >
        <el-card
          shadow="never"
          :data-test="'card-' + c.key"
        >
          <el-statistic
            :title="t('runtime.' + c.key)"
            :formatter="() => c.text"
          />
        </el-card>
      </el-col>
    </el-row>

    <h3
      id="runtime-instances"
      class="cm-section-title"
    >
      {{ t('runtime.instances') }}
    </h3>
    <el-table
      :data="instances"
      row-key="id"
    >
      <el-table-column
        prop="service"
        :label="t('runtime.service')"
        width="140"
      />
      <el-table-column
        prop="id"
        :label="t('runtime.instance')"
        min-width="220"
      />
      <el-table-column
        prop="version"
        :label="t('runtime.version')"
        width="120"
      />
      <el-table-column
        :label="t('runtime.heartbeat')"
        width="180"
      >
        <template #default="{ row }: { row: RuntimeInstance }">
          {{ formatDateTime(row.heartbeat_at) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('runtime.capacity')"
        width="120"
      >
        <template #default="{ row }: { row: RuntimeInstance }">
          {{ capacityOf(row) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('runtime.state')"
        width="110"
      >
        <template #default="{ row }: { row: RuntimeInstance }">
          <el-tag
            :type="instanceState(row).type"
            disable-transitions
          >
            {{ instanceState(row).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        v-if="canWrite"
        :label="t('common.actions')"
        width="110"
      >
        <template #default="{ row }: { row: RuntimeInstance }">
          <el-button
            v-if="!row.stopped_at"
            link
            type="warning"
            :data-test="'drain-' + row.id"
            @click="drain({ instance_id: row.id })"
          >
            {{ t('runtime.drain') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <h3
      id="runtime-leases"
      class="cm-section-title"
    >
      {{ t('runtime.leases') }}
    </h3>
    <el-table
      :data="leases"
      row-key="bot_id"
    >
      <el-table-column
        prop="bot_name"
        :label="t('bots.title')"
        width="180"
      />
      <el-table-column
        :label="t('runtime.holder')"
        min-width="220"
      >
        <template #default="{ row }: { row: RuntimeLease }">
          {{ row.holder_instance ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('runtime.state')"
        width="180"
      >
        <template #default="{ row }: { row: RuntimeLease }">
          <span class="lease-state">
            <el-tag
              :type="leaseState(row)"
              disable-transitions
            >
              {{ t(`runtime.states.${row.connection_state}`) }}
            </el-tag>
            <!-- 排空中的行和「网关刚好挂了」长得一模一样，不打标就没法分辨。 -->
            <el-tag
              v-if="row.drain_requested_by"
              type="warning"
              disable-transitions
            >
              {{ t('runtime.draining') }}
            </el-tag>
          </span>
        </template>
      </el-table-column>
      <el-table-column
        prop="generation"
        :label="t('runtime.generation')"
        width="90"
      />
      <el-table-column
        :label="t('runtime.heartbeat')"
        width="180"
      >
        <template #default="{ row }: { row: RuntimeLease }">
          {{ formatDateTime(row.heartbeat_at) }}
        </template>
      </el-table-column>
      <el-table-column
        v-if="canWrite"
        :label="t('common.actions')"
        width="110"
      >
        <template #default="{ row }: { row: RuntimeLease }">
          <el-button
            link
            type="warning"
            :disabled="!!row.drain_requested_by"
            :data-test="'drain-bot-' + row.bot_key"
            @click="drain({ bot_key: row.bot_key })"
          >
            {{ t('runtime.drain') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <h3
      id="runtime-tasks"
      class="cm-section-title"
    >
      {{ t('runtime.tasks') }}
    </h3>
    <el-table
      :data="tasks"
      row-key="id"
    >
      <el-table-column
        prop="id"
        label="ID"
        width="90"
      />
      <el-table-column
        :label="t('bots.title')"
        width="160"
      >
        <template #default="{ row }: { row: RuntimeTask }">
          {{ row.bot_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('runtime.kind')"
        width="150"
      >
        <template #default="{ row }: { row: RuntimeTask }">
          {{ row.kind }} / {{ row.lane }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('runtime.sessionKey')"
        min-width="160"
      >
        <template #default="{ row }: { row: RuntimeTask }">
          {{ row.session_key ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('runtime.claimedBy')"
        min-width="200"
      >
        <template #default="{ row }: { row: RuntimeTask }">
          {{ row.claimed_by ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('apps.startedAt')"
        width="180"
      >
        <template #default="{ row }: { row: RuntimeTask }">
          {{ formatDateTime(row.started_at) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('runtime.heartbeat')"
        width="180"
      >
        <template #default="{ row }: { row: RuntimeTask }">
          {{ formatDateTime(row.heartbeat_at) }}
        </template>
      </el-table-column>
      <el-table-column
        v-if="canWrite"
        :label="t('common.actions')"
        width="120"
      >
        <template #default="{ row }: { row: RuntimeTask }">
          <el-button
            link
            type="danger"
            :data-test="'cancel-' + row.id"
            @click="cancel(row)"
          >
            {{ t('runtime.cancel') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <h3
      id="runtime-failedOutbox"
      class="cm-section-title"
    >
      {{ t('runtime.failedOutbox') }}
    </h3>
    <el-table
      :data="failedOutbox"
      row-key="id"
    >
      <el-table-column
        prop="id"
        label="ID"
        width="90"
      />
      <el-table-column
        :label="t('bots.title')"
        width="160"
      >
        <template #default="{ row }: { row: RuntimeOutboxItem }">
          {{ row.bot_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        prop="kind"
        :label="t('runtime.kind')"
        width="120"
      />
      <el-table-column
        prop="attempts"
        :label="t('runtime.attempts')"
        width="110"
      />
      <el-table-column
        :label="t('runtime.lastError')"
        min-width="240"
      >
        <template #default="{ row }: { row: RuntimeOutboxItem }">
          {{ row.last_error ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        v-if="canWrite"
        :label="t('common.actions')"
        width="110"
      >
        <template #default="{ row }: { row: RuntimeOutboxItem }">
          <el-button
            link
            type="primary"
            :data-test="'retry-' + row.id"
            @click="retry(row)"
          >
            {{ t('runtime.retry') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
h3 { margin: 24px 0 8px; font-size: 15px; font-weight: 500; }
.lease-state { display: inline-flex; gap: 6px; flex-wrap: wrap; }
</style>
