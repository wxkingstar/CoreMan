<script setup lang="ts">
/**
 * 任务平台：「我的任务」四栏 + 详情。详情顶部一行「现在」写清在谁手上、等什么，下面是给人看的
 * 时间线；给 Agent 看的细节默认折叠，所有人都能展开（展开状态按浏览器记住）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import LoadState from '@/components/LoadState.vue'
import AssigneePicker from '@/components/workTasks/AssigneePicker.vue'
import WorkTaskAsk from '@/components/workTasks/WorkTaskAsk.vue'
import WorkTaskCreateDialog from '@/components/workTasks/WorkTaskCreateDialog.vue'
import WorkTaskCode from '@/components/workTasks/WorkTaskCode.vue'
import WorkTaskStatusIcon from '@/components/workTasks/WorkTaskStatusIcon.vue'
import WorkTaskText from '@/components/workTasks/WorkTaskText.vue'
import {
  workTasks, type AssigneeIn, type WorkTask, type WorkTaskDetail, type WorkTaskStats, type WorkTaskView,
} from '@/api/workTasks'
import { useAuthStore } from '@/stores/auth'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t, te } = useI18n()
const route = useRoute(), router = useRouter()
const auth = useAuthStore()
const PAGE = 50
const VIEWS: (WorkTaskView | 'stats')[] = ['needs_me', 'created', 'watching', 'all', 'stats']
const STATUSES = ['todo', 'in_progress', 'waiting', 'review', 'done', 'cancelled', 'shadow']
const AGENT_OPEN_KEY = 'coreman.workTasks.agentOpen'
const rows = ref<(WorkTask & { id: number })[]>([])
const counts = ref<Record<string, number>>({})
const needsMe = ref(0)
const view = ref<WorkTaskView | 'stats'>('needs_me')
const stats = ref<WorkTaskStats | null>(null)
const status = ref('')
const query = ref('')
const loading = ref(false)
const loadError = ref('')
const more = ref(false)
const detail = ref<WorkTaskDetail | null>(null)
const detailError = ref('')
const detailLoading = ref(false)
const creating = ref(false)
const assigning = ref<'any' | 'bot' | null>(null)
const assignee = ref<AssigneeIn | null>(null)
const assignNote = ref('')
const commentText = ref('')
const busy = ref(false)
const code = computed(() => (route.params.code as string | undefined) ?? '')
const agentOpen = ref<string[]>(readAgentOpen())

function readAgentOpen(): string[] {
  try {
    return JSON.parse(localStorage.getItem(AGENT_OPEN_KEY) ?? '[]') as string[]
  } catch {
    return []
  }
}
watch(agentOpen, value => {
  try { localStorage.setItem(AGENT_OPEN_KEY, JSON.stringify(value)) } catch { /* 无痕模式等：不记 */ }
})

async function load(append = false): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    if (view.value === 'stats') {
      stats.value = await workTasks.stats(30)
      return
    }
    const data = await workTasks.list({
      view: view.value,
      status: status.value || undefined,
      q: query.value.trim() || undefined,
      include_shadow: status.value === 'shadow',
      before_id: append ? rows.value[rows.value.length - 1]?.id : undefined,
      limit: PAGE,
    })
    rows.value = append ? [...rows.value, ...data.items] : data.items
    counts.value = data.counts
    needsMe.value = data.needs_me
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
watch([view, status], () => { void load() })
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
// 列表和详情是同一个路由（`/work-tasks/:code?`）：打开、关闭详情只换参数，列表不重新加载。
function show(row: WorkTask): void { void router.push({ name: 'work-tasks', params: { code: row.code } }) }
function close(): void { void router.push('/work-tasks') }
function pretty(value: unknown): string { return JSON.stringify(value, null, 2) }
function actorLabel(actor: { type: string; name: string } | null): string {
  if (!actor) return t('workTasks.unassigned')
  return actor.type === 'bot' ? `${actor.name}（AI）` : actor.name
}

function updated(next: WorkTaskDetail): void {
  detail.value = next
  void load()
}

async function act(run: () => Promise<WorkTaskDetail>, done?: string): Promise<void> {
  busy.value = true
  try {
    updated(await run())
    if (done) ElMessage.success(done)
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    busy.value = false
  }
}

async function prompt(title: string, required = true): Promise<string | null> {
  try {
    const { value } = await ElMessageBox.prompt(title, {
      inputType: 'textarea',
      inputValidator: (v: string) => !required || !!v?.trim() || t('workTasks.reasonRequired'),
    })
    return (value ?? '').trim()
  } catch {
    return null
  }
}

async function copyDrive(): Promise<void> {
  if (!detail.value) return
  try {
    await navigator.clipboard.writeText(t('workTasks.driveCommand', { code: detail.value.code }))
    ElMessage.success(t('workTasks.copied'))
  } catch {
    ElMessage.info(t('workTasks.driveCommand', { code: detail.value.code }))
  }
}

function startAssign(kind: 'any' | 'bot'): void {
  assignee.value = null
  assignNote.value = ''
  assigning.value = kind
}

async function submitAssign(): Promise<void> {
  const d = detail.value
  if (!d || !assignee.value) return
  const target = assignee.value
  await act(() => workTasks.assign(d.code, { assignee: target, note: assignNote.value.trim() }))
  assigning.value = null
}

async function complete(): Promise<void> {
  const d = detail.value
  if (!d) return
  const text = await prompt(t('workTasks.deliverPrompt'))
  if (text !== null) await act(() => workTasks.complete(d.code, text))
}
async function cancelTask(): Promise<void> {
  const d = detail.value
  if (!d) return
  const text = await prompt(t('workTasks.cancelPrompt'))
  if (text !== null) await act(() => workTasks.cancel(d.code, text))
}
async function reopen(): Promise<void> {
  const d = detail.value
  if (!d) return
  const text = await prompt(t('workTasks.reopenPrompt'), false)
  if (text !== null) await act(() => workTasks.reopen(d.code, text))
}
async function comment(): Promise<void> {
  const d = detail.value
  if (!d || !commentText.value.trim()) return
  const text = commentText.value.trim()
  await act(() => workTasks.comment(d.code, text))
  commentText.value = ''
}

const mine = computed(() => !!detail.value && detail.value.assignee?.type === 'human' && detail.value.assignee.id === auth.user?.id)
const closed = computed(() => !!detail.value && ['done', 'cancelled', 'shadow'].includes(detail.value.status))
const humanEvents = computed(() => (detail.value?.events ?? []).filter(e => e.human_text))
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
              {{ actorLabel(row.assignee) }}
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
    <el-table
      v-else
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
      >
        <template #default="{ row }">
          <span class="code-cell">
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
          {{ actorLabel(row.assignee) }}
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
      @created="d => { updated(d); show(d) }"
    />

    <el-dialog
      :model-value="!!code"
      width="96vw"
      top="2vh"
      class="task-dialog"
      append-to-body
      destroy-on-close
      close-on-press-escape
      data-test="work-task-detail"
      @close="close"
    >
      <template #header>
        <span class="el-dialog__title code-cell">
          <WorkTaskStatusIcon
            v-if="detail"
            :status="detail.status"
          />{{ detail ? `${detail.code} · ${detail.title}` : code }}
        </span>
      </template>
      <LoadState
        :error="detailError"
        @retry="open(code)"
      />
      <div
        v-if="detail"
        v-loading="detailLoading || busy"
        class="task-detail"
      >
        <el-alert
          :type="detail.needs_drive ? 'warning' : 'info'"
          :closable="false"
          class="now"
          data-test="work-task-now"
        >
          <template #title>
            <strong>{{ t('workTasks.now') }}</strong><WorkTaskText
              :text="detail.now"
              :linked="detail.linked"
            />
          </template>
          <el-button
            v-if="detail.needs_drive"
            size="small"
            data-test="copy-drive"
            @click="copyDrive"
          >
            {{ t('workTasks.copyDrive') }}
          </el-button>
        </el-alert>

        <WorkTaskAsk
          v-if="detail.ask?.can_answer"
          :code="detail.code"
          :ask="detail.ask"
          :on-behalf="!detail.ask.mine"
          @answered="updated"
        />

        <div class="task-actions">
          <template v-if="!closed && detail.can_manage && !detail.mirror">
            <el-button
              size="small"
              data-test="assign"
              @click="startAssign('any')"
            >
              {{ detail.assignee ? t('workTasks.handoff') : t('workTasks.assign') }}
            </el-button>
            <el-button
              v-if="detail.assignee?.type !== 'bot'"
              size="small"
              data-test="to-ai"
              @click="startAssign('bot')"
            >
              {{ t('workTasks.toAi') }}
            </el-button>
          </template>
          <el-button
            v-if="mine && !detail.mirror && ['todo', 'in_progress', 'waiting'].includes(detail.status) && !detail.ask"
            size="small"
            type="primary"
            data-test="deliver"
            @click="complete"
          >
            {{ t('workTasks.deliver') }}
          </el-button>
          <el-button
            v-if="!closed && detail.can_manage && !detail.mirror"
            size="small"
            @click="cancelTask"
          >
            {{ t('workTasks.cancel') }}
          </el-button>
          <el-button
            v-if="['done', 'cancelled'].includes(detail.status) && detail.can_manage"
            size="small"
            @click="reopen"
          >
            {{ t('workTasks.reopen') }}
          </el-button>
          <el-button
            size="small"
            @click="act(() => workTasks.watch(detail!.code, !detail!.watching))"
          >
            {{ detail.watching ? t('workTasks.unwatch') : t('workTasks.watch') }}
          </el-button>
        </div>

        <div
          v-if="assigning"
          class="assign-box"
          data-test="assign-box"
        >
          <AssigneePicker
            v-model="assignee"
            :kind="assigning === 'bot' ? 'bot' : 'human'"
            :lock-kind="assigning === 'bot'"
          />
          <el-input
            v-model="assignNote"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 8 }"
            :placeholder="t('workTasks.handoffNote')"
          />
          <p
            v-if="assignee?.type === 'bot'"
            class="muted"
          >
            {{ t('workTasks.manualStartHint') }}
          </p>
          <div class="assign-actions">
            <el-button
              size="small"
              @click="assigning = null"
            >
              {{ t('common.cancel') }}
            </el-button>
            <el-button
              size="small"
              type="primary"
              :disabled="!assignee"
              data-test="assign-submit"
              @click="submitAssign"
            >
              {{ t('workTasks.confirmAssign') }}
            </el-button>
          </div>
        </div>

        <el-descriptions
          :column="2"
          border
          size="small"
        >
          <el-descriptions-item :label="t('workTasks.status')">
            <el-tag :type="statusType(detail.status)">
              {{ statusLabel(detail.status) }}
            </el-tag>
            <el-tag
              v-if="startTag(detail)"
              size="small"
              :type="startTag(detail)?.type"
              effect="plain"
              class="inline-tag"
            >
              {{ startTag(detail)?.text }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.assignee')">
            {{ actorLabel(detail.assignee) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.requester')">
            {{ detail.requester?.name ?? (detail.requester_name || '—') }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.principal')">
            {{ detail.principal?.name ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.reviewer')">
            {{ detail.review_required ? (detail.reviewer?.name ?? detail.principal?.name ?? '—') : t('workTasks.noReview') }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.due')">
            {{ detail.due_at ? formatDateTime(detail.due_at) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item
            v-if="detail.dispatcher"
            :label="t('workTasks.dispatcher')"
          >
            {{ detail.dispatcher.name }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('workTasks.created')">
            {{ formatDateTime(detail.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item
            v-if="detail.parent || detail.related_codes.length"
            :label="t('workTasks.related')"
          >
            <WorkTaskCode
              v-if="detail.parent"
              :code="detail.parent.code"
              :prefix="t('workTasks.parent')"
              :status="detail.linked[detail.parent.code]?.status"
              :title="detail.parent.title"
              class="related"
            />
            <WorkTaskCode
              v-for="item in detail.related_codes"
              :key="item"
              :code="item"
              :status="detail.linked[item]?.status"
              :title="detail.linked[item]?.title"
              class="related"
            />
          </el-descriptions-item>
          <el-descriptions-item
            :label="t('workTasks.summary')"
            :span="2"
          >
            <WorkTaskText
              v-if="detail.summary"
              :text="detail.summary"
              :linked="detail.linked"
            />
            <template v-else>
              —
            </template>
          </el-descriptions-item>
          <el-descriptions-item
            v-if="detail.artifacts.length"
            :label="t('workTasks.artifacts')"
            :span="2"
          >
            <a
              v-for="item in detail.artifacts"
              :key="item.url"
              :href="item.url"
              target="_blank"
              rel="noopener noreferrer"
              class="related"
            >{{ item.title }}</a>
          </el-descriptions-item>
        </el-descriptions>

        <template v-if="detail.children.length">
          <h4>{{ t('workTasks.children') }}</h4>
          <ul class="children">
            <li
              v-for="child in detail.children"
              :key="child.code"
            >
              <WorkTaskCode
                :code="child.code"
                :status="child.status"
                :title="child.title"
              /> {{ child.title }}
              <span class="muted"> · <WorkTaskText
                :text="child.now"
                :linked="detail.linked"
              /></span>
            </li>
          </ul>
        </template>

        <h4>{{ t('workTasks.timeline') }}</h4>
        <el-timeline v-if="humanEvents.length">
          <el-timeline-item
            v-for="event in humanEvents"
            :key="event.id"
            :timestamp="formatDateTime(event.created_at)"
          >
            <div>
              <strong v-if="event.actor_type === 'bot' && event.actor_name">{{ event.actor_name }}：</strong><WorkTaskText
                :text="event.human_text"
                :linked="detail.linked"
              />
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
        <div class="comment-box">
          <el-input
            v-model="commentText"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            maxlength="4000"
            :placeholder="t('workTasks.commentPlaceholder')"
            data-test="comment-input"
          />
          <el-button
            size="small"
            :disabled="!commentText.trim()"
            data-test="comment-submit"
            @click="comment"
          >
            {{ t('workTasks.comment') }}
          </el-button>
        </div>

        <h4>{{ t('workTasks.agentLayer') }}</h4>
        <p class="muted">
          {{ t('workTasks.agentLayerHint') }}
        </p>
        <el-collapse
          v-model="agentOpen"
          data-test="agent-layer"
        >
          <el-collapse-item
            v-if="detail.agent.report"
            :title="t('workTasks.report')"
            name="report"
            data-test="agent-report"
          >
            <pre>{{ detail.agent.report }}</pre>
          </el-collapse-item>
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
            v-if="detail.agent.runs.length"
            :title="t('workTasks.runs', { n: detail.agent.runs.length })"
            name="runs"
          >
            <ol class="runs">
              <li
                v-for="run in detail.agent.runs"
                :key="run.id"
              >
                {{ formatDateTime(run.started_at) }} · {{ run.bot?.name }} ·
                {{ t(`workTasks.triggers.${run.trigger}`, run.trigger) }} ·
                {{ t(`workTasks.runStatuses.${run.status}`, run.status) }}
                <div
                  v-if="run.outcome"
                  class="muted"
                >
                  {{ run.outcome }}
                </div>
                <div class="muted">
                  {{ run.session_key }} · #{{ run.chat_task_id }}
                </div>
              </li>
            </ol>
          </el-collapse-item>
          <el-collapse-item
            :title="t('workTasks.fullEvents')"
            name="events"
          >
            <div
              v-for="event in detail.events"
              :key="event.id"
              class="raw-event"
            >
              <div class="muted">
                {{ formatDateTime(event.created_at) }} · {{ event.kind }}
              </div>
              <div v-if="event.human_text">
                {{ event.human_text }}
              </div>
              <pre v-if="Object.keys(event.agent_detail).length">{{ pretty(event.agent_detail) }}</pre>
            </div>
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
    </el-dialog>
  </section>
</template>

<style scoped>
.tasks-toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; align-items: center; }
.view-badge { margin-left: 4px; vertical-align: middle; }
.view-badge :deep(.el-badge__content) { position: static; transform: none; }
.tasks-table :deep(.el-table__row) { cursor: pointer; }
.tasks-more { display: flex; justify-content: center; margin-top: 12px; }
.stats { display: flex; flex-direction: column; gap: 12px; }
.stat-row { display: flex; flex-wrap: wrap; gap: 48px; }
.title-line { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.now-line { margin-top: 2px; }
.task-detail { display: flex; flex-direction: column; gap: 12px; }
:global(.task-dialog) { max-width: 1600px; margin-bottom: 2vh; }
:global(.task-dialog .el-dialog__body) { max-height: calc(96vh - 64px); overflow-y: auto; }
.task-detail h4 { margin: 8px 0 0; }
.task-detail pre { white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 12px; }
.now :deep(.el-alert__content) { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.task-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.task-actions .el-button + .el-button { margin-left: 0; }
.assign-box { display: flex; flex-direction: column; gap: 8px; padding: 10px; border: 1px solid var(--el-border-color); border-radius: 6px; }
.assign-actions { display: flex; justify-content: flex-end; gap: 6px; }
.comment-box { display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }
.task-messages, .runs, .children { padding-left: 20px; margin: 4px 0; }
.raw-event { padding: 6px 0; border-bottom: 1px dashed var(--el-border-color-lighter); }
.agent-detail { margin-top: 4px; }
.related { margin-right: 8px; }
.code-cell { display: inline-flex; align-items: center; gap: 4px; }
.inline-tag { margin-left: 6px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
