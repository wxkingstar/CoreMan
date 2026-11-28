<script setup lang="ts">
/**
 * 任务页首屏的全貌，和群里的进展卡同一套分块：状态与进度、最新一句、目标（办完是结果和产出）、
 * 现在要人定的、正在做的、接下来的、已完成的。人点开链接先看到这一块，不用自己从属性表和
 * 「经过」里拼。数据是详情接口的 `overview`。
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import WorkTaskActor from './WorkTaskActor.vue'
import WorkTaskAsk from './WorkTaskAsk.vue'
import WorkTaskCode from './WorkTaskCode.vue'
import WorkTaskStatusIcon from './WorkTaskStatusIcon.vue'
import WorkTaskText from './WorkTaskText.vue'
import type { WorkTaskDetail, WorkTaskStep } from '@/api/workTasks'
import { formatClock } from '@/utils/format'

const props = defineProps<{ detail: WorkTaskDetail }>()
const emit = defineEmits<{ answered: [code: string, detail: WorkTaskDetail] }>()
const { t } = useI18n()
const router = useRouter()

// 「已 35 分钟」这类时长跟着时间走：每半分钟重算一次。
const now = ref(Date.now())
const timer = window.setInterval(() => { now.value = Date.now() }, 30_000)
onBeforeUnmount(() => window.clearInterval(timer))

const view = computed(() => props.detail.overview)
const closed = computed(() => ['done', 'cancelled', 'shadow'].includes(props.detail.status))
const stateType = computed(() => {
  const state = view.value.state
  if (state === 'done') return 'success'
  if (state === 'need_human') return 'warning'
  if (state === 'cancelled' || state === 'shadow' || state === 'todo') return 'info'
  return 'primary'
})
const stateText = computed(() => {
  const left = view.value.followups.length
  if (view.value.state === 'done' && left) return t('workTasks.overview.doneWithFollowups', { n: left })
  return t(`workTasks.overview.states.${view.value.state}`)
})
/**
 * 「现在」那句话什么时候还要：办完的看结果，有问题的看下面的问题，在等子任务的看「正在做」，
 * 这些时候它只是重复；其余（谁在处理、等谁验收、还没开工怎么叫）照旧写出来。
 */
const showNow = computed(() => {
  const d = props.detail
  if (d.status === 'shadow') return true
  if (closed.value || view.value.asks.length) return false
  return !(d.agent.waiting_on?.kind === 'task' && view.value.doing.length)
})
/** 这件事自己在跑（没有拆步骤）时，「现在」后面跟几点开始、已经多久。 */
const running = computed(() =>
  props.detail.status === 'in_progress' && props.detail.claimed_at ? props.detail.claimed_at : null,
)
const canAnswer = computed(() => view.value.asks.some(ask => ask.can_answer))
/** 进度条一步一格：做完的、在做的（在等人的另一种颜色）、还没开始的。 */
const segments = computed(() => [
  ...Array.from({ length: view.value.steps_done }, () => 'done'),
  ...view.value.doing.map(step => (step.kind === 'waiting_human' ? 'waiting' : 'doing')),
  ...view.value.plan.map(() => 'todo'),
])
const segmentsLabel = computed(() => t('workTasks.overview.segments', {
  done: view.value.steps_done, doing: view.value.doing.length, todo: view.value.plan.length,
}))
/**
 * 这一页最要紧的那句话，只有一句用大字：有最新进展就是它，办完了是结果，否则是「现在」那句。
 */
const lead = computed(() => {
  if (view.value.latest) return 'latest'
  if (props.detail.status === 'done' && props.detail.summary) return 'result'
  return showNow.value ? 'now' : ''
})

function duration(from: string, to: number | string): string {
  const end = typeof to === 'number' ? to : new Date(to).getTime()
  const minutes = Math.floor((end - new Date(from).getTime()) / 60_000)
  if (!Number.isFinite(minutes) || minutes < 1) return t('workTasks.overview.durLt1')
  if (minutes < 60) return t('workTasks.overview.durMin', { m: minutes })
  const hours = Math.floor(minutes / 60)
  if (hours >= 48) return t('workTasks.overview.durDay', { d: Math.floor(hours / 24) })
  return minutes % 60
    ? t('workTasks.overview.durHourMin', { h: hours, m: minutes % 60 })
    : t('workTasks.overview.durHour', { h: hours })
}
function started(from: string): string {
  return t('workTasks.overview.started', { at: formatClock(from, now.value) })
}
function elapsed(from: string): string {
  return t('workTasks.overview.elapsed', { d: duration(from, now.value) })
}
function took(step: WorkTaskStep): string {
  return step.closed_at ? t('workTasks.overview.took', { d: duration(step.since, step.closed_at) }) : ''
}
function open(step: WorkTaskStep): void {
  void router.push({ name: 'work-task', params: { code: step.code } })
}
async function copyDrive(): Promise<void> {
  const command = t('workTasks.driveCommand', { code: props.detail.code })
  try {
    await navigator.clipboard.writeText(command)
    ElMessage.success(t('workTasks.copied'))
  } catch {
    ElMessage.info(command)
  }
}
</script>

<template>
  <section
    class="overview"
    data-test="work-task-overview"
  >
    <div class="headline">
      <el-tag
        :type="stateType"
        effect="dark"
        disable-transitions
        data-test="overview-state"
      >
        {{ stateText }}
      </el-tag>
      <template v-if="view.steps_total">
        <span
          class="segments"
          role="img"
          :aria-label="segmentsLabel"
          :title="segmentsLabel"
        ><span
          v-for="(kind, index) in segments"
          :key="index"
          class="segment"
          :class="kind"
        /></span>
        <span
          class="count"
          data-test="overview-progress"
        >{{ t('workTasks.overview.progress', { done: view.steps_done, total: view.steps_total }) }}</span>
      </template>
    </div>

    <div
      v-if="showNow"
      class="now"
      :class="{ lead: lead === 'now', drive: detail.needs_drive }"
      data-test="work-task-now"
    >
      <span class="now-text"><WorkTaskText
        :text="detail.now"
        :linked="detail.linked"
      /></span>
      <span
        v-if="running"
        class="sub"
      >{{ started(running) }}，{{ elapsed(running) }}</span>
      <el-button
        v-if="detail.needs_drive"
        size="small"
        data-test="copy-drive"
        @click="copyDrive"
      >
        {{ t('workTasks.copyDrive') }}
      </el-button>
    </div>

    <div
      v-if="view.latest"
      class="lead"
      data-test="overview-latest"
    >
      <WorkTaskText
        :text="view.latest.text"
        :linked="detail.linked"
      />
      <span class="sub">{{ view.latest.actor_name
        ? t('workTasks.overview.latestBy', { name: view.latest.actor_name, at: formatClock(view.latest.at, now) })
        : t('workTasks.overview.latestAt', { at: formatClock(view.latest.at, now) }) }}</span>
    </div>

    <div
      v-if="detail.summary || detail.artifacts.length"
      class="block"
      data-test="overview-goal"
    >
      <h4 class="label">
        {{ detail.status === 'done' ? t('workTasks.overview.result') : t('workTasks.overview.goal') }}
      </h4>
      <p
        v-if="detail.summary"
        :class="{ lead: lead === 'result', done: lead === 'result' }"
      >
        <WorkTaskText
          :text="detail.summary"
          :linked="detail.linked"
        />
      </p>
      <p
        v-if="detail.artifacts.length"
        class="artifacts"
      >
        <span class="sub">{{ t('workTasks.artifacts') }}</span>
        <a
          v-for="item in detail.artifacts"
          :key="item.url"
          :href="item.url"
          target="_blank"
          rel="noopener noreferrer"
        >{{ item.title }}</a>
      </p>
    </div>

    <div
      v-if="view.asks.length"
      class="block"
      data-test="overview-asks"
    >
      <h4
        class="label"
        :class="{ attention: canAnswer }"
      >
        {{ canAnswer ? t('workTasks.overview.needYou') : t('workTasks.overview.needOthers') }}
      </h4>
      <div class="asks">
        <template
          v-for="ask in view.asks"
          :key="ask.id"
        >
          <WorkTaskAsk
            v-if="ask.can_answer"
            :code="ask.code"
            :ask="ask"
            :on-behalf="!ask.mine"
            @answered="d => emit('answered', ask.code, d)"
          />
          <p
            v-else
            class="waiting-line"
          >
            <WorkTaskStatusIcon status="waiting" />
            <span><span class="wait">{{ t('workTasks.overview.waitingAnswer', { name: ask.to?.name ?? '' }) }}</span>{{ ask.question }}</span>
          </p>
        </template>
      </div>
    </div>

    <!-- 一张清单：三列对齐（编号、内容、时间），还要人处理的、正在做的、接下来的、已完成的依次往下排。 -->
    <div
      v-if="view.followups.length || view.doing.length || view.plan.length || view.done.length"
      class="ledger"
    >
      <section
        v-if="view.followups.length"
        class="group"
        data-test="overview-followups"
      >
        <h4 class="label attention">
          {{ t('workTasks.overview.followups') }}<span class="n">{{ view.followups.length }}</span>
        </h4>
        <ul>
          <li
            v-for="(item, index) in view.followups"
            :key="index"
            class="row plain"
          >
            <span class="c-code"><span class="box attention" /></span>
            <span class="c-main">{{ item }}</span>
          </li>
        </ul>
      </section>

      <section
        v-if="view.doing.length"
        class="group"
        data-test="overview-doing"
      >
        <h4 class="label">
          {{ t('workTasks.overview.doing') }}<span class="n">{{ view.doing.length }}</span>
        </h4>
        <ul>
          <li
            v-for="step in view.doing"
            :key="step.code"
            class="row"
            :data-back-key="step.code"
            data-test="overview-step"
            @click="open(step)"
          >
            <WorkTaskCode
              class="c-code"
              :code="step.code"
              :status="step.status"
              :title="step.title"
              @click.stop
            />
            <div class="c-main">
              <div class="title-line">
                <span class="step-title">{{ step.title }}</span>
                <WorkTaskActor
                  v-if="step.assignee"
                  class="who"
                  :actor="step.assignee"
                />
              </div>
              <div class="note">
                <span
                  v-if="step.kind === 'waiting_human'"
                  class="wait"
                >{{ step.note }}</span>
                <span v-if="step.status !== 'todo'">{{ started(step.since) }}<span class="narrow-only">，{{ elapsed(step.since) }}</span></span>
                <span v-if="step.kind === 'running' && step.note">
                  <template v-if="step.note_at">{{ t('workTasks.overview.recent') }}</template><WorkTaskText
                    :text="step.note"
                    :linked="detail.linked"
                  /><template v-if="step.note_at">（{{ formatClock(step.note_at, now) }}）</template>
                </span>
              </div>
            </div>
            <span
              v-if="step.status !== 'todo'"
              class="c-side"
            >{{ elapsed(step.since) }}</span>
          </li>
        </ul>
      </section>

      <section
        v-if="view.plan.length"
        class="group"
        data-test="overview-plan"
      >
        <h4 class="label">
          {{ t('workTasks.overview.next') }}<span class="n">{{ view.plan.length }}</span>
        </h4>
        <ul>
          <li
            v-for="(item, index) in view.plan"
            :key="index"
            class="row plain"
          >
            <span class="c-code"><span class="box" /></span>
            <span class="c-main">{{ item }}</span>
          </li>
        </ul>
      </section>

      <section
        v-if="view.done.length"
        class="group"
        data-test="overview-done"
      >
        <h4 class="label">
          {{ t('workTasks.overview.finished') }}<span class="n">{{ view.done.length }}</span>
        </h4>
        <ul>
          <li
            v-for="step in view.done"
            :key="step.code"
            class="row"
            :class="{ cancelled: step.kind === 'cancelled' }"
            :data-back-key="step.code"
            data-test="overview-step"
            @click="open(step)"
          >
            <WorkTaskCode
              class="c-code"
              :code="step.code"
              :status="step.status"
              :title="step.title"
              @click.stop
            />
            <div class="c-main">
              <div class="title-line">
                <span class="step-title"><template v-if="step.question && step.kind === 'done'">{{ t('workTasks.overview.decided', { name: step.assignee?.name ?? '' }) }}</template>{{ step.title }}</span>
                <WorkTaskActor
                  v-if="step.assignee && !step.question"
                  class="who"
                  :actor="step.assignee"
                />
                <span
                  v-if="step.kind === 'done' && !step.question && took(step)"
                  class="who narrow-only"
                >{{ took(step) }}</span>
              </div>
              <div
                v-if="step.note"
                class="note"
                :class="{ answer: step.question }"
              >
                <span><template v-if="step.question">{{ t('workTasks.overview.answer') }}</template><WorkTaskText
                  :text="step.note"
                  :linked="detail.linked"
                /></span>
              </div>
            </div>
            <span
              v-if="step.kind === 'cancelled'"
              class="c-side"
            >{{ t('workTasks.statuses.cancelled') }}</span>
            <span
              v-else-if="!step.question && took(step)"
              class="c-side"
            >{{ took(step) }}</span>
          </li>
        </ul>
      </section>
    </div>
  </section>
</template>

<style scoped>
/*
 * 字号四级：18 最要紧的那一句，15 正文，13 次要信息和小标题，数字等宽对齐。
 * 间距按 4 的倍数：块与块 20，小标题到内容 6，清单每行上下 9。
 */
.overview { display: flex; flex-direction: column; gap: 20px; font-variant-numeric: tabular-nums; }
.sub, .count, .note, .c-side, .who { color: var(--el-text-color-secondary); font-size: 13px; }

.headline { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 12px; }
.segments { display: flex; gap: 3px; flex: 1 1 120px; max-width: 260px; }
.segment { flex: 1 1 0; min-width: 2px; height: 8px; border-radius: 2px; background: var(--el-border-color); }
.segment.done { background: var(--el-color-primary); }
.segment.doing { background: var(--el-color-primary-light-5); }
.segment.waiting { background: var(--el-color-warning); }

/* 最要紧的那一句：左边一道品牌色的竖线，字比正文大一级。 */
.lead {
  margin: 0; padding: 2px 0 2px 14px; border-left: 3px solid var(--el-color-primary);
  font-size: 18px; line-height: 1.55; font-weight: 500; letter-spacing: .01em; text-wrap: pretty;
}
.lead .sub { display: block; margin-top: 4px; font-weight: 400; letter-spacing: 0; }
.lead.done { border-left-color: var(--el-color-success); }
.now { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; }
.now:not(.lead) { padding: 8px 12px; border-radius: 8px; background: var(--el-fill-color-light); }
.now.lead .sub { display: inline; margin: 0; }
.now.drive { border-left-color: var(--el-color-warning); }
.now.drive:not(.lead) { background: var(--el-color-warning-light-9); }

.label {
  display: flex; align-items: baseline; gap: 6px; margin: 0 0 6px;
  font-size: 13px; line-height: 1.5; font-weight: 600; color: var(--el-text-color-secondary);
}
.label .n { font-weight: 400; color: var(--el-text-color-placeholder); }
.label.attention { color: var(--el-color-warning); }
.block p { margin: 0; }
.artifacts { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; margin-top: 6px; font-size: 14px; }
.asks { display: flex; flex-direction: column; gap: 8px; }
.waiting-line { display: flex; align-items: baseline; gap: 8px; }
.wait { color: var(--el-color-warning); }

/* 清单：各组共用三列，编号、标题、时间从上到下都对得齐。 */
.ledger { display: grid; grid-template-columns: max-content minmax(0, 1fr) max-content; column-gap: 14px; }
.group, .group > ul { display: contents; }
.group > .label { grid-column: 1 / -1; margin: 20px 0 2px; }
.group:first-child > .label { margin-top: 0; }
.row {
  display: grid; grid-template-columns: subgrid; grid-column: 1 / -1; align-items: baseline;
  list-style: none; padding: 9px 10px; margin: 0 -10px; border-radius: 8px; cursor: pointer;
}
.row:hover { background: var(--el-fill-color-light); }
.row.plain { cursor: default; padding-block: 6px; }
.row.plain:hover { background: none; }
.c-code { grid-column: 1; }
.c-main { grid-column: 2; min-width: 0; }
.c-side { grid-column: 3; justify-self: end; white-space: nowrap; }
.title-line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; }
.step-title { font-weight: 500; }
.note { display: flex; flex-wrap: wrap; gap: 0 14px; margin-top: 2px; line-height: 1.6; }
.note.answer { color: var(--el-text-color-regular); }
.row.cancelled .step-title { color: var(--el-text-color-secondary); font-weight: 400; }
/* 待办的小方框：和上面状态图标一样大、站在同一列。 */
.box {
  display: inline-block; width: 16px; height: 16px; margin: 0 1px; vertical-align: -3px;
  border: 1.5px solid var(--el-text-color-placeholder); border-radius: 4px; background: var(--el-bg-color);
}
.box.attention { border-color: var(--el-color-warning); }
.narrow-only { display: none; }

@media (max-width: 640px) {
  .overview { gap: 16px; }
  .lead { font-size: 17px; }
  /* 手机上两列：右边那列时间收进内容里（接在开始时间、执行人后面）。 */
  .ledger { grid-template-columns: max-content minmax(0, 1fr); column-gap: 10px; }
  .c-side { display: none; }
  .narrow-only { display: inline; }
}
</style>
