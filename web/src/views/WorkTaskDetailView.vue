<script setup lang="ts">
/**
 * 单个任务的独立页面（`/work-tasks/:code`）：不套管理台布局，飞书里点开就是这一页，加载快；
 * 顶部可以回到任务列表。上面全是给人看的；给 AI 看的细节收在底部、默认折叠。
 * 地址带 `?format=md` 时只输出纯文本（Markdown），给 AI 读。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import LoadState from '@/components/LoadState.vue'
import AssigneePicker from '@/components/workTasks/AssigneePicker.vue'
import WorkTaskActor from '@/components/workTasks/WorkTaskActor.vue'
import WorkTaskAsk from '@/components/workTasks/WorkTaskAsk.vue'
import WorkTaskCode from '@/components/workTasks/WorkTaskCode.vue'
import WorkTaskStatusIcon from '@/components/workTasks/WorkTaskStatusIcon.vue'
import WorkTaskText from '@/components/workTasks/WorkTaskText.vue'
import { workTasks, type AssigneeIn, type WorkTask, type WorkTaskDetail } from '@/api/workTasks'
import { usePageMemory } from '@/composables/usePageMemory'
import { useAuthStore } from '@/stores/auth'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t, te } = useI18n()
const route = useRoute(), router = useRouter()
const auth = useAuthStore()
const code = computed(() => String(route.params.code ?? ''))
const plain = computed(() => route.query.format === 'md')
const detail = ref<WorkTaskDetail | null>(null)
const text = ref('')
const loadError = ref('')
const loading = ref(false)
const busy = ref(false)
const assigning = ref<'any' | 'bot' | null>(null)
const assignee = ref<AssigneeIn | null>(null)
const assignNote = ref('')
const commentText = ref('')
const allEvents = ref(false)
const agentOpen = ref<string[]>([])
// 点进别的任务再返回：展开过的「显示全部」和给 AI 看的细节原样恢复，滚回原处、闪一下刚才点的地方。
const memory = usePageMemory(() => ({ allEvents: allEvents.value, agentOpen: agentOpen.value }))
// 手机（飞书里点开多半是手机）：信息表一栏、子任务表把进展折进标题下面。
const narrowQuery = window.matchMedia?.('(max-width: 640px)')
const narrow = ref(!!narrowQuery?.matches)
const onNarrow = (e: MediaQueryListEvent): void => { narrow.value = e.matches }
narrowQuery?.addEventListener?.('change', onNarrow)
onBeforeUnmount(() => narrowQuery?.removeEventListener?.('change', onNarrow))

async function load(): Promise<void> {
  loadError.value = ''
  loading.value = true
  try {
    if (plain.value) text.value = await workTasks.markdown(code.value)
    else detail.value = await workTasks.get(code.value)
  } catch (e) {
    loadError.value = errorMessage(e)
  } finally {
    loading.value = false
  }
  await nextTick()
  memory.ready()
}
watch([code, plain], () => {
  detail.value = null
  text.value = ''
  assigning.value = null
  const saved = memory.recall()
  allEvents.value = saved?.allEvents ?? false
  agentOpen.value = saved?.agentOpen ?? []
  void load()
}, { immediate: true })
watch([detail, code], () => {
  document.title = detail.value ? `${detail.value.code} ${detail.value.title}` : code.value
}, { immediate: true })

/** 从列表点进来的就退回去（保留原来那一栏和筛选），直接打开链接的跳到列表。 */
function back(): void {
  const from = (window.history.state as { back?: string } | null)?.back ?? ''
  if (from.startsWith('/work-tasks') && !from.startsWith(`/work-tasks/`)) router.back()
  else void router.push({ name: 'work-tasks' })
}

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
function startTag(row: WorkTask): { text: string; type: 'warning' | 'info' | 'success' } | null {
  if (row.assignee?.type !== 'bot' || ['done', 'cancelled', 'shadow'].includes(row.status)) return null
  if (row.needs_drive) return { text: t('workTasks.needsDrive'), type: 'warning' }
  if (row.start_mode === 'auto') return { text: t('workTasks.autoStart'), type: 'info' }
  return { text: t('workTasks.claimed'), type: 'success' }
}
function pretty(value: unknown): string { return JSON.stringify(value, null, 2) }
function openChild(row: WorkTask, _column: unknown, event: Event): void {
  memory.markOrigin(row.code, event)
  void router.push({ name: 'work-task', params: { code: row.code } })
}

async function act(run: () => Promise<WorkTaskDetail>, done?: string): Promise<void> {
  busy.value = true
  try {
    detail.value = await run()
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
  const value = await prompt(t('workTasks.deliverPrompt'))
  if (value !== null) await act(() => workTasks.complete(d.code, value))
}
async function cancelTask(): Promise<void> {
  const d = detail.value
  if (!d) return
  const value = await prompt(t('workTasks.cancelPrompt'))
  if (value !== null) await act(() => workTasks.cancel(d.code, value))
}
async function reopen(): Promise<void> {
  const d = detail.value
  if (!d) return
  const value = await prompt(t('workTasks.reopenPrompt'), false)
  if (value !== null) await act(() => workTasks.reopen(d.code, value))
}
async function comment(): Promise<void> {
  const d = detail.value
  if (!d || !commentText.value.trim()) return
  const value = commentText.value.trim()
  await act(() => workTasks.comment(d.code, value))
  commentText.value = ''
}

const mine = computed(() => !!detail.value && detail.value.assignee?.type === 'human' && detail.value.assignee.id === auth.user?.id)
const closed = computed(() => !!detail.value && ['done', 'cancelled', 'shadow'].includes(detail.value.status))
const humanEvents = computed(() => (detail.value?.events ?? []).filter(e => e.human_text))
/** 「经过」默认只放里程碑；AI 员工开始/继续处理、拆子任务（表里有）这类流水点「显示全部」才出来。 */
const shownEvents = computed(() => allEvents.value ? humanEvents.value : humanEvents.value.filter(e => e.milestone))
const hiddenEvents = computed(() => humanEvents.value.length - humanEvents.value.filter(e => e.milestone).length)
</script>

<template>
  <pre
    v-if="plain"
    class="plain"
    data-test="work-task-plain"
  >{{ loadError || text }}</pre>
  <div
    v-else
    class="task-page"
    data-test="work-task-detail"
  >
    <header class="task-head">
      <el-button
        text
        :icon="ArrowLeft"
        data-test="back-to-list"
        @click="back"
      >
        {{ t('workTasks.backToList') }}
      </el-button>
      <h1 class="code-cell">
        <WorkTaskStatusIcon
          v-if="detail"
          :status="detail.status"
        />{{ detail ? `${detail.code} · ${detail.title}` : code }}
      </h1>
    </header>
    <LoadState
      :error="loadError"
      @retry="load()"
    />
    <div
      v-if="!detail && loading"
      v-loading="true"
      class="task-loading"
    />
    <div
      v-if="detail"
      v-loading="busy"
      class="task-detail cm-panel"
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
        @answered="d => detail = d"
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
        :column="narrow ? 1 : 2"
        border
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
          <WorkTaskActor :actor="detail.assignee" />
        </el-descriptions-item>
        <el-descriptions-item :label="t('workTasks.requester')">
          <WorkTaskActor
            :actor="detail.requester"
            :empty="detail.requester_name || '—'"
          />
        </el-descriptions-item>
        <el-descriptions-item :label="t('workTasks.principal')">
          <WorkTaskActor
            :actor="detail.principal"
            empty="—"
          />
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
          <WorkTaskActor :actor="{ type: 'bot', name: detail.dispatcher.name }" />
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
          :span="narrow ? 1 : 2"
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
          :span="narrow ? 1 : 2"
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
        <h4>{{ t('workTasks.children') }}（{{ detail.children.length }}）</h4>
        <el-table
          :data="detail.children"
          row-key="code"
          class="children"
          data-test="work-task-children"
          @row-click="openChild"
        >
          <el-table-column
            :label="t('workTasks.code')"
            width="90"
          >
            <template #default="{ row }">
              <WorkTaskCode
                :code="row.code"
                :status="row.status"
                :title="row.title"
                @click.stop
              />
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.taskTitle')"
            :min-width="narrow ? 180 : 220"
          >
            <template #default="{ row }">
              {{ row.title }}
              <WorkTaskText
                v-if="narrow"
                class="muted child-now"
                :text="row.now"
                :linked="detail.linked"
              />
            </template>
          </el-table-column>
          <el-table-column
            :label="t('workTasks.status')"
            width="90"
          >
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="statusType(row.status)"
              >
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            v-if="!narrow"
            :label="t('workTasks.assignee')"
            min-width="150"
          >
            <template #default="{ row }">
              <WorkTaskActor :actor="row.assignee" />
            </template>
          </el-table-column>
          <el-table-column
            v-if="!narrow"
            :label="t('workTasks.progress')"
            min-width="320"
          >
            <template #default="{ row }">
              <WorkTaskText
                class="muted"
                :text="row.now"
                :linked="detail.linked"
              />
            </template>
          </el-table-column>
        </el-table>
      </template>

      <div class="timeline-head">
        <h4>{{ t('workTasks.timeline') }}</h4>
        <el-button
          v-if="hiddenEvents"
          link
          type="primary"
          size="small"
          data-test="all-events"
          @click="allEvents = !allEvents"
        >
          {{ allEvents ? t('workTasks.milestonesOnly') : t('workTasks.allEvents', { n: hiddenEvents }) }}
        </el-button>
      </div>
      <el-timeline v-if="shownEvents.length">
        <el-timeline-item
          v-for="event in shownEvents"
          :key="event.id"
          :timestamp="formatDateTime(event.created_at)"
        >
          <strong
            v-if="event.actor_type === 'bot' && event.actor_name"
            class="event-actor"
          ><WorkTaskActor :actor="{ type: 'bot', name: event.actor_name }" />：</strong><WorkTaskText
            :text="event.human_text"
            :linked="detail.linked"
          />
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
        <router-link
          :to="{ name: 'work-task', params: { code: detail.code }, query: { format: 'md' } }"
          target="_blank"
          data-test="plain-link"
        >
          {{ t('workTasks.plainText') }}
        </router-link>
      </p>
      <!-- 给 AI 看的细节：每次打开都收起，AI 员工自己会读。 -->
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
  </div>
</template>

<style scoped>
.plain { margin: 0; padding: 16px; white-space: pre-wrap; word-break: break-word; font-size: 14px; line-height: 1.7; }
/* 正文 15px（原来表格和说明多是 12px，读起来吃力）；够宽就铺开，两边留白随屏宽变化。 */
.task-page {
  max-width: 1760px; margin: 0 auto; padding: 16px clamp(12px, 3vw, 48px) 40px;
  min-height: 100vh; box-sizing: border-box; font-size: 15px; line-height: 1.7;
}
.task-page :deep(.el-descriptions), .task-page :deep(.el-table), .task-page :deep(.el-timeline),
.task-page :deep(.el-collapse-item__content), .task-page :deep(.el-alert__title) { font-size: 15px; }
.task-page :deep(.el-descriptions__label) { width: 7em; }
.task-page :deep(.el-timeline-item__timestamp) { font-size: 13px; }
.task-head { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; margin-bottom: 12px; }
.task-head h1 { margin: 0; font-size: 22px; line-height: 1.4; }
.task-head .code-cell { gap: 6px; }
.task-loading { height: 240px; }
.task-detail { display: flex; flex-direction: column; gap: 12px; }
.task-detail h4 { margin: 8px 0 0; font-size: 17px; }
.timeline-head { display: flex; align-items: baseline; gap: 12px; }
.task-detail pre { white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 13px; }
.event-actor { display: inline-flex; align-items: center; }
.now :deep(.el-alert__content) { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.task-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.task-actions .el-button + .el-button { margin-left: 0; }
.assign-box { display: flex; flex-direction: column; gap: 8px; padding: 10px; border: 1px solid var(--el-border-color); border-radius: 6px; }
.assign-actions { display: flex; justify-content: flex-end; gap: 6px; }
.comment-box { display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }
.children :deep(.el-table__row) { cursor: pointer; }
.child-now { display: block; margin-top: 2px; }
@media (max-width: 640px) {
  .task-detail.cm-panel { padding: 12px; }
  .task-head h1 { font-size: 19px; }
}
.task-messages, .runs { padding-left: 20px; margin: 4px 0; }
.raw-event { padding: 6px 0; border-bottom: 1px dashed var(--el-border-color-lighter); }
.related { margin-right: 8px; }
.code-cell { display: inline-flex; align-items: center; gap: 4px; }
.inline-tag { margin-left: 6px; }
.muted { color: var(--el-text-color-secondary); font-size: 14px; }
</style>
