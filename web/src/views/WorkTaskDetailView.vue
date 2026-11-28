<script setup lang="ts">
/**
 * 单个任务的独立页面（`/work-tasks/:code`）：不套管理台布局，飞书里点开就是这一页，加载快；
 * 顶部可以回到任务列表。首屏是全貌（和群里的进展卡同一套分块），往下是经过和评论；
 * 任务信息、给 AI 看的细节收在底部、默认折叠。
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
import WorkTaskCode from '@/components/workTasks/WorkTaskCode.vue'
import WorkTaskOverview from '@/components/workTasks/WorkTaskOverview.vue'
import WorkTaskTerm from '@/components/workTasks/WorkTaskTerm.vue'
import WorkTaskStatusIcon from '@/components/workTasks/WorkTaskStatusIcon.vue'
import WorkTaskText from '@/components/workTasks/WorkTaskText.vue'
import { workTasks, type AssigneeIn, type WorkTask, type WorkTaskDetail } from '@/api/workTasks'
import { usePageMemory } from '@/composables/usePageMemory'
import { useAuthStore } from '@/stores/auth'
import { errorMessage } from '@/utils/errors'
import { formatClock, formatDateTime } from '@/utils/format'

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
// 交给 AI 员工时默认马上开工。
const assignStart = ref(true)
const commentText = ref('')
const allEvents = ref(false)
const agentOpen = ref<string[]>([])
const infoOpen = ref<string[]>([])
// 点进别的任务再返回：展开过的「显示全部」、任务信息和给 AI 看的细节原样恢复，滚回原处、闪一下刚才点的地方。
const memory = usePageMemory(() => ({ allEvents: allEvents.value, agentOpen: agentOpen.value, infoOpen: infoOpen.value }))
// 够宽时分两栏，任务信息在右边那一栏里，默认展开；一栏时（手机、窄窗口）收起来。
const wideQuery = window.matchMedia?.('(min-width: 1100px)')
const wide = ref(!!wideQuery?.matches)
const onWide = (e: MediaQueryListEvent): void => { wide.value = e.matches }
wideQuery?.addEventListener?.('change', onWide)
onBeforeUnmount(() => wideQuery?.removeEventListener?.('change', onWide))

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
  infoOpen.value = saved?.infoOpen ?? (wide.value ? ['info'] : [])
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
function startTag(row: WorkTask): { text: string; type: 'warning' | 'info' | 'success'; term: string } | null {
  if (row.assignee?.type !== 'bot' || ['done', 'cancelled', 'shadow'].includes(row.status)) return null
  if (row.needs_drive) return { text: t('workTasks.needsDrive'), type: 'warning', term: 'needsDrive' }
  if (row.start_mode === 'auto') return { text: t('workTasks.autoStart'), type: 'info', term: 'autoStart' }
  return { text: t('workTasks.claimed'), type: 'success', term: 'claimed' }
}
function pretty(value: unknown): string { return JSON.stringify(value, null, 2) }

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

function startAssign(kind: 'any' | 'bot'): void {
  assignee.value = null
  assignNote.value = ''
  assignStart.value = true
  assigning.value = kind
}

async function submitAssign(): Promise<void> {
  const d = detail.value
  if (!d || !assignee.value) return
  const target = assignee.value
  const start = target.type === 'bot' && assignStart.value
  await act(() => workTasks.assign(d.code, { assignee: target, note: assignNote.value.trim(), start }))
  if (detail.value?.start_error) {
    ElMessage.warning({ message: t('workTasks.startFailed', { reason: detail.value.start_error }), duration: 8000 })
  }
  assigning.value = null
}
/** 叫 AI 员工开工或接着做：和在飞书里对它说「做 编号」一样，以点的人的身份做。 */
async function startTask(): Promise<void> {
  const d = detail.value
  if (d) await act(() => workTasks.start(d.code), t('workTasks.started'))
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
/** 答的是这件事自己的问题就直接换成返回的详情；答的是下面某一步的问题，重新取这一页。 */
function answered(answeredCode: string, next: WorkTaskDetail): void {
  if (answeredCode === detail.value?.code) detail.value = next
  else void load()
}
async function comment(): Promise<void> {
  const d = detail.value
  if (!d || !commentText.value.trim()) return
  const value = commentText.value.trim()
  await act(() => workTasks.comment(d.code, value))
  commentText.value = ''
}

const mine = computed(() => !!detail.value && detail.value.assignee?.type === 'human' && detail.value.assignee.id === auth.user?.id)
/** 标题前的状态图标：在等子任务、而子任务都在跑时，对人来说就是「进行中」，和首屏的状态一致。 */
const headStatus = computed(() => {
  const d = detail.value
  if (!d) return ''
  return d.status === 'waiting' && d.overview.state === 'running' ? 'in_progress' : d.status
})
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
      <div class="task-title-row">
        <div class="task-title">
          <h1>
            <WorkTaskStatusIcon
              v-if="detail"
              :status="headStatus"
            /><span class="head-code">{{ detail ? detail.code : code }}</span><span v-if="detail">{{ detail.title }}</span>
          </h1>
          <p
            v-if="detail"
            class="task-meta"
            data-test="work-task-meta"
          >
            <span class="pair"><WorkTaskTerm term="assignee" /><WorkTaskActor :actor="detail.assignee" /></span>
            <WorkTaskTerm
              v-if="detail.needs_drive"
              term="needsDrive"
              bare
            >
              <el-tag
                size="small"
                type="warning"
                effect="plain"
                disable-transitions
              >
                {{ t('workTasks.needsDrive') }}
              </el-tag>
            </WorkTaskTerm>
            <span class="pair"><WorkTaskTerm term="requester" /><WorkTaskActor
              :actor="detail.requester"
              :empty="detail.requester_name || '—'"
            /></span>
            <el-tag
              v-if="detail.priority !== 'normal'"
              size="small"
              :type="detail.priority === 'urgent' ? 'danger' : 'warning'"
              effect="plain"
              disable-transitions
            >
              {{ t('workTasks.priority') }} {{ t(`workTasks.priorities.${detail.priority}`) }}
            </el-tag>
            <span
              v-if="detail.due_at"
              class="pair"
            ><span>{{ t('workTasks.due') }}</span><span class="value">{{ formatDateTime(detail.due_at) }}</span></span>
            <WorkTaskCode
              v-if="detail.parent"
              :code="detail.parent.code"
              :prefix="t('workTasks.parent')"
              :status="detail.linked[detail.parent.code]?.status"
              :title="detail.parent.title"
            />
          </p>
        </div>
        <!-- 操作按钮：够宽时在标题右边；一栏时挪到全貌下面，不占手机的第一屏。 -->
        <Teleport
          v-if="detail"
          to="#work-task-actions-slot"
          defer
          :disabled="wide"
        >
          <div class="task-actions-wrap">
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
              <template v-if="assignee?.type === 'bot'">
                <el-switch
                  v-model="assignStart"
                  :active-text="t('workTasks.startNow')"
                  data-test="assign-start"
                />
                <p class="muted">
                  {{ assignStart ? t('workTasks.startNowHint') : t('workTasks.manualStartHint') }}
                </p>
              </template>
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
          </div>
        </Teleport>
      </div>
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
    <!-- 够宽时两栏：左边是全貌，右边是经过、评论和任务信息；窄屏从上到下排成一列。 -->
    <div
      v-if="detail"
      v-loading="busy"
      class="task-body"
    >
      <section class="task-detail cm-panel task-main">
        <WorkTaskOverview
          :detail="detail"
          @answered="answered"
          @start="startTask"
        />
      </section>
      <div
        id="work-task-actions-slot"
        class="task-actions-slot"
      />
      <aside class="task-side">
        <section class="task-detail cm-panel">
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
          <ol
            v-if="shownEvents.length"
            class="events"
            data-test="work-task-timeline"
          >
            <li
              v-for="event in shownEvents"
              :key="event.id"
              class="event"
            >
              <time
                class="event-at"
                :datetime="event.created_at"
                :title="formatDateTime(event.created_at)"
              >{{ formatClock(event.created_at) }}</time>
              <div class="event-text">
                <strong
                  v-if="event.actor_type === 'bot' && event.actor_name"
                  class="event-actor"
                ><WorkTaskActor :actor="{ type: 'bot', name: event.actor_name }" />：</strong><WorkTaskText
                  :text="event.human_text"
                  :linked="detail.linked"
                />
              </div>
            </li>
          </ol>
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
        </section>
        <section class="task-detail cm-panel">
          <!-- 任务信息：谁发起、代表谁、谁验收这些，平时用不上：一栏时收起来，两栏时在右边展开。 -->
          <el-collapse
            v-model="infoOpen"
            class="task-info"
            data-test="task-info"
          >
            <el-collapse-item
              :title="t('workTasks.overview.info')"
              name="info"
            >
              <dl class="info">
                <div class="info-row">
                  <dt>{{ t('workTasks.status') }}</dt>
                  <dd>
                    <el-tag
                      :type="statusType(headStatus)"
                      size="small"
                      disable-transitions
                    >
                      {{ statusLabel(headStatus) }}
                    </el-tag>
                    <WorkTaskTerm
                      v-if="startTag(detail)"
                      :term="startTag(detail)!.term"
                      bare
                    >
                      <el-tag
                        size="small"
                        :type="startTag(detail)?.type"
                        effect="plain"
                        disable-transitions
                      >
                        {{ startTag(detail)?.text }}
                      </el-tag>
                    </WorkTaskTerm>
                  </dd>
                </div>
                <div class="info-row">
                  <dt><WorkTaskTerm term="assignee" /></dt>
                  <dd><WorkTaskActor :actor="detail.assignee" /></dd>
                </div>
                <div class="info-row">
                  <dt><WorkTaskTerm term="requester" /></dt>
                  <dd>
                    <WorkTaskActor
                      :actor="detail.requester"
                      :empty="detail.requester_name || '—'"
                    />
                  </dd>
                </div>
                <div class="info-row">
                  <dt><WorkTaskTerm term="principal" /></dt>
                  <dd>
                    <WorkTaskActor
                      :actor="detail.principal"
                      empty="—"
                    />
                  </dd>
                </div>
                <div class="info-row">
                  <dt><WorkTaskTerm term="reviewer" /></dt>
                  <dd>{{ detail.review_required ? (detail.reviewer?.name ?? detail.principal?.name ?? '—') : t('workTasks.noReview') }}</dd>
                </div>
                <div
                  v-if="detail.dispatcher"
                  class="info-row"
                >
                  <dt><WorkTaskTerm term="dispatcher" /></dt>
                  <dd><WorkTaskActor :actor="{ type: 'bot', name: detail.dispatcher.name }" /></dd>
                </div>
                <div class="info-row">
                  <dt>{{ t('workTasks.due') }}</dt>
                  <dd>{{ detail.due_at ? formatDateTime(detail.due_at) : '—' }}</dd>
                </div>
                <div class="info-row">
                  <dt>{{ t('workTasks.created') }}</dt>
                  <dd>{{ formatDateTime(detail.created_at) }}</dd>
                </div>
                <div
                  v-if="detail.related_codes.length"
                  class="info-row"
                >
                  <dt>{{ t('workTasks.related') }}</dt>
                  <dd>
                    <WorkTaskCode
                      v-for="item in detail.related_codes"
                      :key="item"
                      :code="item"
                      :status="detail.linked[item]?.status"
                      :title="detail.linked[item]?.title"
                    />
                  </dd>
                </div>
              </dl>
            </el-collapse-item>
          </el-collapse>
        </section>
      </aside>
      <section class="task-detail cm-panel task-agent">
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
      </section>
    </div>
  </div>
</template>

<style scoped>
.plain { margin: 0; padding: 16px; white-space: pre-wrap; word-break: break-word; font-size: 14px; line-height: 1.7; }
/*
 * 字号：标题 22，栏目标题 15（加粗），正文 15，次要信息 13；数字等宽。
 * 够宽就铺开，两边留白随屏宽变化。
 */
.task-page {
  max-width: 1760px; margin: 0 auto; padding: 16px clamp(12px, 3vw, 48px) 40px;
  min-height: 100vh; box-sizing: border-box; font-size: 15px; line-height: 1.7; font-variant-numeric: tabular-nums;
}
.task-head { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; margin-bottom: 16px; }
.task-head > .el-button { margin-left: -8px; }
.task-title-row { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: 8px 16px; width: 100%; }
.task-title { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.task-head h1 { display: flex; align-items: baseline; gap: 10px; margin: 0; font-size: 22px; line-height: 1.4; font-weight: 650; letter-spacing: -.01em; }
.task-head h1 :deep(.task-status-icon) { align-self: center; flex-shrink: 0; }
.head-code { color: var(--el-text-color-secondary); font-weight: 500; white-space: nowrap; }
.task-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 20px; margin: 0; font-size: 13px; color: var(--el-text-color-secondary); }
.task-meta .pair { display: inline-flex; align-items: center; gap: 8px; }
.task-meta .pair > :last-child { color: var(--el-text-color-primary); }
.task-loading { height: 240px; }

.task-body { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-items: start; }
.task-body > *, .task-side > * { margin-bottom: 0; min-width: 0; }
.task-side { display: flex; flex-direction: column; gap: 16px; }
/* 够宽：全貌占左边大半，经过和任务信息在右边；给 AI 看的细节接在全貌下面。右栏比左栏高时，多出来的高度留给下面那一行。 */
@media (min-width: 1100px) {
  .task-body { grid-template-columns: minmax(0, 1.7fr) minmax(360px, 1fr); grid-template-rows: auto 1fr; }
  .task-main { grid-column: 1; grid-row: 1; }
  .task-side { grid-column: 2; grid-row: 1 / span 2; }
  .task-agent { grid-column: 1; grid-row: 2; }
  /* 指派框从按钮下面弹出来，不把下面的内容往下挤。 */
  .task-actions-wrap { position: relative; align-items: flex-end; }
  .task-actions-wrap .assign-box {
    position: absolute; top: calc(100% + 8px); right: 0; z-index: 10; width: 440px; max-width: 90vw;
    box-sizing: border-box; background: var(--cm-surface); box-shadow: var(--cm-shadow);
  }
}
.task-detail { display: flex; flex-direction: column; gap: 14px; padding: 20px 24px; }
.task-detail h4 { margin: 0; font-size: 15px; line-height: 1.6; font-weight: 600; }
.task-detail pre { white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 13px; line-height: 1.65; }
.muted { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }

/* 经过：时间一列靠右对齐，中间一条细线串起来，右边是内容。 */
.timeline-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.events { display: grid; grid-template-columns: max-content minmax(0, 1fr); column-gap: 14px; margin: 0; padding: 0; list-style: none; }
.event { display: grid; grid-template-columns: subgrid; grid-column: 1 / -1; }
.event-at { padding-top: 1px; text-align: right; font-size: 13px; color: var(--el-text-color-secondary); white-space: nowrap; }
.event-text { position: relative; padding: 0 0 14px 18px; border-left: 1px solid var(--el-border-color); line-height: 1.6; }
.event-text::before {
  content: ''; position: absolute; left: -4px; top: 8px; width: 7px; height: 7px; border-radius: 50%;
  background: var(--el-bg-color); border: 1.5px solid var(--el-text-color-placeholder); box-sizing: border-box;
}
.event:last-child .event-text { padding-bottom: 2px; border-left-color: transparent; }
.event:last-child .event-text::before { background: var(--el-color-primary); border-color: var(--el-color-primary); }
.event-actor { display: inline-flex; align-items: center; font-weight: 600; }
.comment-box { display: flex; flex-direction: column; gap: 8px; align-items: flex-end; margin-top: 2px; }

/* 折叠块在面板里：不再套一层边距和边线，标题和栏目标题一样。 */
.task-detail :deep(.el-collapse) { padding: 0; border: 0; --el-collapse-header-height: 44px; }
.task-detail :deep(.el-collapse-item__header) { font-size: 14px; }
.task-detail :deep(.el-collapse-item__content) { font-size: 14px; padding-bottom: 14px; }
.task-detail :deep(.el-collapse-item:last-child .el-collapse-item__header),
.task-detail :deep(.el-collapse-item:last-child .el-collapse-item__wrap) { border-bottom: 0; }
.task-info :deep(.el-collapse-item__header) { height: auto; min-height: 0; line-height: 1.6; font-size: 15px; border-bottom: 0; }
.task-info :deep(.el-collapse-item__content) { padding: 10px 0 0; font-size: 15px; }
.info { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 10px 20px; margin: 0; }
.info-row { display: grid; grid-template-columns: subgrid; grid-column: 1 / -1; align-items: baseline; }
.info dt { color: var(--el-text-color-secondary); font-size: 13px; }
.info dd { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin: 0; min-width: 0; }

.task-actions-wrap { display: flex; flex-direction: column; gap: 8px; }
.task-actions-slot:empty { display: none; }
.task-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.task-actions .el-button + .el-button { margin-left: 0; }
.assign-box { display: flex; flex-direction: column; gap: 8px; padding: 12px; border: 1px solid var(--el-border-color); border-radius: 10px; }
.assign-actions { display: flex; justify-content: flex-end; gap: 8px; }
.task-messages, .runs { padding-left: 20px; margin: 4px 0; }
.runs li + li { margin-top: 8px; }
.raw-event { padding: 8px 0; border-bottom: 1px dashed var(--el-border-color-lighter); }
@media (max-width: 640px) {
  .task-detail.cm-panel { padding: 14px; }
  .task-body, .task-side { gap: 12px; }
  /* 手机上编号和标题连着排，标题长了自然折行。 */
  .task-head h1 { display: block; font-size: 19px; }
  .task-head h1 :deep(.task-status-icon) { margin-right: 6px; }
  .head-code { margin-right: 8px; }
}
</style>
