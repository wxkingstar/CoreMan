<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import LoadState from '@/components/LoadState.vue'
import { workTasks, type WorkTask, type WorkTaskDetail } from '@/api/workTasks'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t, te } = useI18n()
const route = useRoute(), router = useRouter()
const PAGE = 50
const rows = ref<(WorkTask & { id: number })[]>([])
const counts = ref<Record<string, number>>({})
const status = ref('')
const query = ref('')
const loading = ref(false)
const loadError = ref('')
const more = ref(false)
const detail = ref<WorkTaskDetail | null>(null)
const detailError = ref('')
const detailLoading = ref(false)
const code = computed(() => (route.params.code as string | undefined) ?? '')
const STATUSES = ['shadow', 'pending', 'queued', 'running', 'awaiting_confirmation', 'done', 'unfinished', 'cancelled', 'needs_human']

async function load(append = false): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const data = await workTasks.list({
      status: status.value || undefined,
      q: query.value.trim() || undefined,
      before_id: append ? rows.value[rows.value.length - 1]?.id : undefined,
      limit: PAGE,
    })
    rows.value = append ? [...rows.value, ...data.items] : data.items
    counts.value = data.counts
    more.value = data.items.length === PAGE
  } catch (e) {
    loadError.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

async function open(value: string): Promise<void> {
  detail.value = null
  detailError.value = ''
  if (!value) return
  detailLoading.value = true
  try {
    detail.value = await workTasks.get(value)
  } catch (e) {
    detailError.value = errorMessage(e)
  } finally {
    detailLoading.value = false
  }
}

onMounted(() => { void load(); void open(code.value) })
watch(status, () => { void load() })
watch(code, value => { void open(value) })

function statusLabel(value: string): string {
  const key = `workTasks.statuses.${value}`
  return te(key) ? t(key) : value
}
function outcomeLabel(value: string): string {
  const key = `groupObserve.outcomes.${value}`
  return te(key) ? t(key) : value
}
function statusType(value: string): 'info' | 'warning' | 'success' | 'danger' | 'primary' {
  if (value === 'done') return 'success'
  if (value === 'unfinished' || value === 'needs_human') return 'danger'
  if (value === 'running' || value === 'awaiting_confirmation') return 'warning'
  if (value === 'shadow') return 'info'
  return 'primary'
}
function show(row: WorkTask): void { void router.push({ name: 'work-task', params: { code: row.code } }) }
function close(): void { void router.push({ name: 'work-tasks' }) }
function pretty(value: unknown): string { return JSON.stringify(value, null, 2) }
</script>

<template>
  <section class="cm-panel">
    <p class="panel-hint">
      {{ t('workTasks.hint') }}
    </p>
    <div class="tasks-toolbar">
      <el-select
        v-model="status"
        clearable
        :placeholder="t('workTasks.allStatuses')"
        style="width: 200px"
      >
        <el-option
          v-for="value in STATUSES"
          :key="value"
          :value="value"
          :label="`${statusLabel(value)}（${counts[value] ?? 0}）`"
        />
      </el-select>
      <el-input
        v-model="query"
        clearable
        :placeholder="t('workTasks.search')"
        style="width: 260px"
        @keyup.enter="load()"
        @clear="load()"
      />
      <el-button @click="load()">
        {{ t('common.refresh') }}
      </el-button>
    </div>
    <LoadState
      :error="loadError"
      @retry="load()"
    />
    <el-table
      v-loading="loading"
      :data="rows"
      row-key="code"
      :empty-text="t('workTasks.empty')"
      class="tasks-table"
      @row-click="show"
    >
      <el-table-column
        :label="t('workTasks.code')"
        width="100"
        prop="code"
      />
      <el-table-column
        :label="t('workTasks.taskTitle')"
        min-width="220"
        show-overflow-tooltip
      >
        <template #default="{ row }">
          <strong>{{ row.title }}</strong>
          <div class="muted">
            {{ row.summary }}
          </div>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('workTasks.status')"
        width="120"
      >
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('workTasks.assignee')"
        width="140"
      >
        <template #default="{ row }">
          {{ row.assignee?.name ?? t('workTasks.unassigned') }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('workTasks.requester')"
        width="120"
        prop="requester_name"
      />
      <el-table-column
        :label="t('workTasks.created')"
        width="170"
      >
        <template #default="{ row }">
          {{ formatDateTime(row.created_at) }}
        </template>
      </el-table-column>
    </el-table>
    <div
      v-if="more"
      class="tasks-more"
    >
      <el-button
        :loading="loading"
        @click="load(true)"
      >
        {{ t('groupObserve.loadMore') }}
      </el-button>
    </div>

    <el-drawer
      :model-value="!!code"
      size="min(720px, 100%)"
      :title="detail ? `${detail.code} · ${detail.title}` : code"
      data-test="work-task-detail"
      @close="close"
    >
      <LoadState
        :error="detailError"
        @retry="open(code)"
      />
      <div
        v-if="detail"
        v-loading="detailLoading"
        class="task-detail"
      >
        <el-descriptions
          :column="2"
          border
        >
          <el-descriptions-item :label="t('workTasks.status')">
            <el-tag :type="statusType(detail.status)">
              {{ statusLabel(detail.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.assignee')">
            {{ detail.assignee?.name ?? t('workTasks.unassigned') }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.requester')">
            {{ detail.requester_name || '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.dispatcher')">
            {{ detail.dispatcher?.name ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.created')">
            {{ formatDateTime(detail.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.related')">
            <template v-if="detail.related_codes.length">
              <router-link
                v-for="item in detail.related_codes"
                :key="item"
                :to="{ name: 'work-task', params: { code: item } }"
                class="related"
              >
                {{ item }}
              </router-link>
            </template>
            <span v-else>—</span>
          </el-descriptions-item>
          <el-descriptions-item
            :label="t('workTasks.summary')"
            :span="2"
          >
            {{ detail.summary || '—' }}
          </el-descriptions-item>
        </el-descriptions>

        <h4>{{ t('workTasks.timeline') }}</h4>
        <el-timeline v-if="detail.events.length">
          <el-timeline-item
            v-for="event in detail.events"
            :key="event.id"
            :timestamp="formatDateTime(event.created_at)"
          >
            <div>
              <strong v-if="event.actor_name">{{ event.actor_name }}：</strong>{{ event.human_text }}
            </div>
            <el-collapse
              v-if="Object.keys(event.agent_detail).length"
              class="agent-detail"
            >
              <el-collapse-item :title="t('workTasks.detail')">
                <pre>{{ pretty(event.agent_detail) }}</pre>
              </el-collapse-item>
            </el-collapse>
          </el-timeline-item>
        </el-timeline>
        <el-empty
          v-else
          :description="t('workTasks.noEvents')"
        />

        <h4>{{ t('workTasks.agentLayer') }}</h4>
        <el-collapse data-test="agent-layer">
          <el-collapse-item
            v-if="detail.agent.brief"
            :title="t('workTasks.brief')"
            name="brief"
          >
            <pre>{{ detail.agent.brief }}</pre>
          </el-collapse-item>
          <el-collapse-item
            v-if="detail.agent.digest"
            :title="t('workTasks.digest')"
            name="digest"
          >
            <pre>{{ detail.agent.digest }}</pre>
          </el-collapse-item>
          <el-collapse-item
            v-if="detail.agent.decision"
            :title="t('workTasks.decision')"
            name="decision"
          >
            <p>
              {{ outcomeLabel(detail.agent.decision.outcome) }} ·
              {{ t('workTasks.probability') }}
              {{ detail.agent.decision.probability === null ? '—' : detail.agent.decision.probability.toFixed(2) }}
            </p>
            <h5>{{ t('workTasks.recentMessages') }}</h5>
            <ol class="task-messages">
              <li
                v-for="(line, index) in detail.agent.decision.recent_messages"
                :key="index"
              >
                <strong>{{ line.sender }}</strong>：{{ line.text }}
              </li>
            </ol>
            <h5>{{ t('workTasks.dispatchRaw') }}</h5>
            <pre>{{ pretty(detail.agent.decision.dispatch) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </el-drawer>
  </section>
</template>

<style scoped>
.tasks-toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.tasks-table :deep(.el-table__row) { cursor: pointer; }
.tasks-more { display: flex; justify-content: center; margin-top: 12px; }
.task-detail h4 { margin: 20px 0 8px; }
.task-detail pre { white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 12px; }
.task-messages { padding-left: 20px; margin: 4px 0 12px; }
.agent-detail { margin-top: 4px; }
.related { margin-right: 8px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
