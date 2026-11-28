<script setup lang="ts">
/**
 * 任务平台：「我的任务」四栏 + 统计。点开一个任务进它的独立页面（WorkTaskDetailView）；
 * 当前那一栏和状态筛选记在地址里，从详情页返回时还停在原处。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import LoadState from '@/components/LoadState.vue'
import WorkTaskActor from '@/components/workTasks/WorkTaskActor.vue'
import WorkTaskAsk from '@/components/workTasks/WorkTaskAsk.vue'
import WorkTaskCreateDialog from '@/components/workTasks/WorkTaskCreateDialog.vue'
import WorkTaskStatusIcon from '@/components/workTasks/WorkTaskStatusIcon.vue'
import { workTasks, type WorkTask, type WorkTaskDetail, type WorkTaskStats, type WorkTaskView } from '@/api/workTasks'
import { usePageMemory } from '@/composables/usePageMemory'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t, te } = useI18n()
const route = useRoute(), router = useRouter()
const PAGE = 50
const VIEWS: (WorkTaskView | 'stats')[] = ['needs_me', 'created', 'watching', 'all', 'stats']
const STATUSES = ['todo', 'in_progress', 'waiting', 'review', 'done', 'cancelled', 'shadow']
const rows = ref<(WorkTask & { id: number })[]>([])
const counts = ref<Record<string, number>>({})
const needsMe = ref(0)
const view = ref<WorkTaskView | 'stats'>(VIEWS.includes(route.query.view as WorkTaskView) ? route.query.view as WorkTaskView : 'needs_me')
const stats = ref<WorkTaskStats | null>(null)
const status = ref(STATUSES.includes(route.query.status as string) ? route.query.status as string : '')
const query = ref('')
// 自动记下的接口任务、求助默认不列，要看时勾上（记在地址里）。
const records = ref(route.query.records === '1')
// 首页（待我处理）下面再列一段：我发起、还在进行的事，不用我处理也看得到进展到哪了。
const mine = ref<(WorkTask & { id: number })[]>([])
const MINE_LIMIT = 20
const loading = ref(false)
const loadError = ref('')
const more = ref(false)
const creating = ref(false)
// 返回列表时回到原处：已经加载的行（含「加载更多」）、搜索词和统计都原样放回，再悄悄刷新。
interface ListMemory {
  rows: (WorkTask & { id: number })[]; counts: Record<string, number>; needsMe: number
  more: boolean; query: string; stats: WorkTaskStats | null; mine?: (WorkTask & { id: number })[]
}
const memory = usePageMemory<ListMemory>(() => ({
  rows: rows.value, counts: counts.value, needsMe: needsMe.value,
  more: more.value, query: query.value, stats: stats.value, mine: mine.value,
}))
/** 首页那一段「我发起、还在进行的」只在没筛选、没搜索时出现。 */
const home = computed(() => view.value === 'needs_me' && !status.value && !query.value.trim())
const sections = computed(() => [
  { key: 'main', title: '', rows: rows.value },
  ...(home.value && mine.value.length ? [{ key: 'mine', title: t('workTasks.myOpen'), rows: mine.value }] : []),
])

async function load(append = false, quiet = false): Promise<void> {
  loading.value = !quiet
  loadError.value = ''
  try {
    if (view.value === 'stats') {
      stats.value = await workTasks.stats(30)
      return
    }
    const [data, started] = await Promise.all([
      workTasks.list({
        view: view.value,
        status: status.value || undefined,
        q: query.value.trim() || undefined,
        include_shadow: status.value === 'shadow',
        records: records.value || undefined,
        before_id: append ? rows.value[rows.value.length - 1]?.id : undefined,
        // 悄悄刷新时按已经加载的行数取，页面不会突然变短。
        limit: quiet ? Math.min(200, Math.max(PAGE, rows.value.length)) : PAGE,
      }),
      home.value && !append ? workTasks.list({ view: 'created', unfinished: true, limit: MINE_LIMIT }) : null,
    ])
    rows.value = append ? [...rows.value, ...data.items] : data.items
    counts.value = data.counts
    needsMe.value = data.needs_me
    if (!quiet) more.value = data.items.length === PAGE
    if (!append) {
      // 已经列在「待我处理」里的不再重复。
      const shown = new Set(rows.value.map(row => row.code))
      mine.value = (started?.items ?? []).filter(row => !shown.has(row.code))
    }
  } catch (e) {
    loadError.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  const saved = memory.recall()
  if (!saved) {
    await load()
    memory.ready()
    return
  }
  stats.value = saved.stats
  rows.value = saved.rows
  counts.value = saved.counts
  needsMe.value = saved.needsMe
  more.value = saved.more
  query.value = saved.query
  mine.value = saved.mine ?? []
  await nextTick()
  memory.ready()
  void load(false, true)
})
watch([view, status, records], () => {
  // 记在地址里：从详情页返回时还停在这一栏。
  const q = {
    ...(view.value === 'needs_me' ? {} : { view: view.value }),
    ...(status.value ? { status: status.value } : {}),
    ...(records.value ? { records: '1' } : {}),
  }
  void router.replace({ name: 'work-tasks', query: q })
  void load()
})

function statusLabel(value: string): string {
  const key = `workTasks.statuses.${value}`
  return te(key) ? t(key) : value
}
function statusType(value: string): 'info' | 'warning' | 'success' | 'danger' | 'primary' {
  if (value === 'done') return 'success'
  if (value === 'review' || value === 'waiting') return 'warning'
  if (value === 'shadow' || value === 'cancelled') return 'info'
  return 'primary'
}
/** 开工方式：人一眼看出要不要自己去叫 AI 员工开工。 */
/** 顶层的事下面收着的步骤和决策点：列表里只写个数，点开才展开。 */
function insideLine(row: WorkTask): string {
  const parts: string[] = []
  if (row.steps) parts.push(t('workTasks.insideSteps', { done: row.steps_done ?? 0, n: row.steps }))
  if (row.decisions) {
    parts.push(
      row.decisions_open
        ? t('workTasks.insideDecisionsOpen', { n: row.decisions, open: row.decisions_open })
        : t('workTasks.insideDecisions', { n: row.decisions }),
    )
  }
  return parts.join(' · ')
}

function startTag(row: WorkTask): { text: string; type: 'warning' | 'info' | 'success' } | null {
  if (row.assignee?.type !== 'bot' || ['done', 'cancelled', 'shadow'].includes(row.status)) return null
  if (row.needs_drive) return { text: t('workTasks.needsDrive'), type: 'warning' }
  if (row.start_mode === 'auto') return { text: t('workTasks.autoStart'), type: 'info' }
  return { text: t('workTasks.claimed'), type: 'success' }
}
function show(row: WorkTask, _column?: unknown, event?: Event): void {
  memory.markOrigin(row.code, event)
  void router.push({ name: 'work-task', params: { code: row.code } })
}

</script>

<template>
  <section class="cm-panel">
    <p class="panel-hint">
      {{ t('workTasks.hint') }}
    </p>
    <div class="tasks-toolbar">
      <el-radio-group
        v-model="view"
        data-test="work-task-views"
      >
        <el-radio-button
          v-for="item in VIEWS"
          :key="item"
          :value="item"
        >
          {{ t(`workTasks.views.${item}`) }}
          <el-badge
            v-if="item === 'needs_me' && needsMe"
            :value="needsMe"
            class="view-badge"
          />
        </el-radio-button>
      </el-radio-group>
      <el-select
        v-model="status"
        clearable
        :placeholder="t('workTasks.allStatuses')"
        style="width: 160px"
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
        style="width: 220px"
        @keyup.enter="load()"
        @clear="load()"
      />
      <el-tooltip
        v-if="view !== 'needs_me' && view !== 'stats'"
        :content="t('workTasks.showRecordsHint')"
        placement="top"
      >
        <el-checkbox
          v-model="records"
          data-test="show-records"
        >
          {{ t('workTasks.showRecords') }}
        </el-checkbox>
      </el-tooltip>
      <el-button @click="load()">
        {{ t('common.refresh') }}
      </el-button>
      <el-button
        type="primary"
        data-test="work-task-new"
        @click="creating = true"
      >
        {{ t('workTasks.create') }}
      </el-button>
    </div>
    <LoadState
      :error="loadError"
      @retry="load()"
    />
    <div
      v-if="view === 'stats'"
      v-loading="loading"
      class="stats"
      data-test="work-task-stats"
    >
      <template v-if="stats">
        <div class="stat-row">
          <el-statistic
            :title="t('workTasks.statHandoffs')"
            :value="stats.handoffs_per_task ?? 0"
            :precision="2"
          />
          <el-statistic
            :title="t('workTasks.statFirstPass', { n: stats.agent_reviewed })"
            :value="stats.agent_first_pass_rate === null ? 0 : stats.agent_first_pass_rate * 100"
            :precision="1"
            suffix="%"
          />
        </div>
        <h4>{{ t('workTasks.statBySource', { days: stats.days }) }}</h4>
        <el-table :data="stats.by_source">
          <el-table-column :label="t('workTasks.source')">
            <template #default="{ row }">
              {{ t(`workTasks.sources.${row.source}`, row.source) }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.statCreated')"
            prop="created"
          />
          <el-table-column
            :label="t('workTasks.statDone')"
            prop="done"
          />
          <el-table-column :label="t('workTasks.statAvgHours')">
            <template #default="{ row }">
              {{ row.avg_hours ?? '—' }}
            </template>
          </el-table-column>
        </el-table>
        <h4>{{ t('workTasks.statBacklog') }}</h4>
        <el-table :data="stats.backlog">
          <el-table-column :label="t('workTasks.assignee')">
            <template #default="{ row }">
              <WorkTaskActor :actor="row.assignee" />
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.statOpen')"
            prop="open"
          />
          <el-table-column
            :label="t('workTasks.statOverdue')"
            prop="overdue"
          />
        </el-table>
      </template>
    </div>
    <template v-else>
      <div
        v-for="section in sections"
        :key="section.key"
        class="tasks-section"
        :data-test="`tasks-${section.key}`"
      >
        <h4 v-if="section.title">
          {{ section.title }}
        </h4>
        <el-table
          v-loading="loading && section.key === 'main'"
          :data="section.rows"
          row-key="code"
          :empty-text="home ? t('workTasks.nothingForMe') : t('workTasks.empty')"
          class="tasks-table"
          @row-click="show"
        >
          <el-table-column
            :label="t('workTasks.code')"
            width="100"
          >
            <template #default="{ row }">
              <span
                class="code-cell"
                :data-back-key="row.code"
              >
                <WorkTaskStatusIcon :status="row.status" />{{ row.code }}
              </span>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.taskTitle')"
            min-width="300"
          >
            <template #default="{ row }">
              <div class="title-line">
                <strong>{{ row.title }}</strong>
                <el-tag
                  v-if="startTag(row)"
                  size="small"
                  :type="startTag(row)?.type"
                  effect="plain"
                >
                  {{ startTag(row)?.text }}
                </el-tag>
                <el-tag
                  v-if="row.mirror"
                  size="small"
                  type="info"
                  effect="plain"
                >
                  {{ t(`workTasks.sources.${row.source}`, row.source) }}
                </el-tag>
                <el-tag
                  v-if="row.priority !== 'normal'"
                  size="small"
                  type="danger"
                  effect="plain"
                >
                  {{ t(`workTasks.priorities.${row.priority}`) }}
                </el-tag>
              </div>
              <div class="muted now-line">
                {{ row.now }}
              </div>
              <div
                v-if="row.steps || row.decisions"
                class="muted now-line"
                data-test="work-task-inside"
              >
                {{ insideLine(row) }}
              </div>
              <WorkTaskAsk
                v-if="row.ask?.mine"
                :code="row.code"
                :ask="row.ask"
                compact
                @answered="load()"
              />
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.status')"
            width="100"
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
              <WorkTaskActor :actor="row.assignee" />
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.requester')"
            width="110"
          >
            <template #default="{ row }">
              {{ row.requester?.name ?? row.requester_name }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.due')"
            width="150"
          >
            <template #default="{ row }">
              {{ row.due_at ? formatDateTime(row.due_at) : '—' }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.created')"
            width="170"
            data-test="col-created"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.created_at) }}
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>
    <div
      v-if="more && view !== 'stats'"
      class="tasks-more"
    >
      <el-button
        :loading="loading"
        @click="load(true)"
      >
        {{ t('groupObserve.loadMore') }}
      </el-button>
    </div>

    <WorkTaskCreateDialog
      v-model="creating"
      @created="(d: WorkTaskDetail) => show(d)"
    />
  </section>
</template>

<style scoped>
.tasks-toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; align-items: center; }
.view-badge { margin-left: 4px; vertical-align: middle; }
.view-badge :deep(.el-badge__content) { position: static; transform: none; }
.tasks-table :deep(.el-table__row) { cursor: pointer; }
.tasks-more { display: flex; justify-content: center; margin-top: 12px; }
.tasks-section + .tasks-section { margin-top: 24px; }
.tasks-section h4 { margin: 0 0 8px; font-size: 15px; }
.stats { display: flex; flex-direction: column; gap: 12px; }
.stat-row { display: flex; flex-wrap: wrap; gap: 48px; }
.title-line { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.now-line { margin-top: 2px; }
.code-cell { display: inline-flex; align-items: center; gap: 4px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
