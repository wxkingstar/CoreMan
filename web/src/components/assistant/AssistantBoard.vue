<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { QuestionFilled } from '@element-plus/icons-vue'
import { BOARD_GROUPS, DISPATCH_TOOL, PROXIED_TOOLS, myAssistant, type Board, type BoardGap, type BoardGroup, type BoardItem } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import FollowupActionDialog, { type FollowupAction } from '@/components/assistant/FollowupActionDialog.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const GROUPS = BOARD_GROUPS
/** 状态说明：每组标题旁的「?」看这一组，顶部「状态说明」一次看完五种。 */
const guideOpen = ref(false)
const data = ref<Board | null>(null)
const error = ref(''), loading = ref(false)
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.board() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
// 与卡片同一说法：挂着的子任务是调度公共员工的说公共员工在办，子任务找不到了说助理在办；
// 同事的名字读不出来时说同事在办。
function handler(item: BoardItem) {
  if (item.handler === 'job') {
    if (!item.job) return t('assistant.board.handler.assistant')
    if (item.job.kind === 'bot_help') return t('assistant.board.handler.botHelp')
  }
  if (item.handler === 'person' && (item.locked || !item.handler_name)) return t('assistant.board.handler.colleague')
  return t('assistant.board.handler.' + item.handler, { name: item.handler_name ?? '' })
}
async function decline(gap: BoardGap) {
  try {
    await ElMessageBox.confirm(t('assistant.board.gap.declineConfirm', { name: gap.id }), t('assistant.board.gap.decline'), { type: 'warning' })
  } catch { return }
  try {
    await myAssistant.declineGap(gap.id)
    ElMessage.success(t('assistant.board.gap.declined'))
    await load()
  } catch (e) { ElMessage.error(errorMessage(e)) }
}
const level = (gap: BoardGap) => t('assistant.board.gap.levels.' + (gap.need_write ? 'write' : 'read'))
async function grant(gap: BoardGap) {
  try {
    await ElMessageBox.confirm(t('assistant.board.gap.grantConfirm', { name: gap.key, value: level(gap) }), t('assistant.board.gap.grant'), { type: 'info' })
  } catch { return }
  try {
    const done = await myAssistant.grantGap(gap.id)
    // 只有缺口真的解决了才说开通了：授权写进去了但级别仍不够时如实说。
    if (done.status === 'resolved') ElMessage.success(t('assistant.board.gap.granted'))
    else ElMessage.warning(t('assistant.board.gap.grantIncomplete'))
    await load()
  } catch (e) { ElMessage.error(errorMessage(e)) }
}
// 理由由所有者自己写（会给平台管理员看）：不预填助理记下的需要，那是只给所有者看的内容。
const REASON_MAX = 300
async function request(gap: BoardGap) {
  let reason: string
  try {
    const { value } = await ElMessageBox.prompt(t('assistant.board.gap.reasonPrompt'), t('assistant.board.gap.request'), {
      inputValidator: (text: string) => (text.trim().length > 0 && text.trim().length <= REASON_MAX) || t('assistant.board.gap.reasonInvalid'),
    })
    reason = value
  } catch { return }
  try {
    await myAssistant.requestGap(gap.id, reason.trim())
    ElMessage.success(t('assistant.board.gap.requestSent'))
    await load()
  } catch (e) { ElMessage.error(errorMessage(e)) }
}
async function withdraw(gap: BoardGap) {
  try {
    await ElMessageBox.confirm(t('assistant.board.gap.withdrawConfirm', { name: gap.id }), t('assistant.board.gap.withdraw'), { type: 'warning' })
  } catch { return }
  try {
    await myAssistant.withdrawGap(gap.id)
    ElMessage.success(t('assistant.board.gap.withdrawn'))
    await load()
  } catch (e) { ElMessage.error(errorMessage(e)) }
}
/** 这一组的件数；「等你」连同待批的单一起算。 */
const count = (g: BoardGroup) => (data.value ? data.value.counts[g] + (g === 'owner' ? data.value.approvals_total : 0) : 0)
const chipClass = (g: BoardGroup) => ({ 'is-zero': !count(g), 'is-primary': g === 'owner' && count(g) > 0, 'is-alert': g === 'stalled' && count(g) > 0 })
/** 列在这一组的缺口（所有者自己能开的在等你，已申请的在等别人，要找管理员的在停滞）。 */
const gapsIn = (g: BoardGroup) => (data.value?.gaps ?? []).filter((gap) => (gap.stage ?? 'owner') === g)
const isEmpty = (g: BoardGroup) => !data.value?.groups[g].length && !gapsIn(g).length && !(g === 'owner' && data.value?.approvals.length)
const sections = ref<Partial<Record<BoardGroup, HTMLElement>>>({})
function jump(g: BoardGroup) {
  sections.value[g]?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
/** 期限到了或过了标红，三个工作日以上没进展标黄。 */
const overdue = (item: BoardItem) => !!item.due_on && !!data.value && item.due_on <= data.value.today
/** 原因标签的颜色：要你动手的蓝，该催的黄，停滞的红，其余灰。 */
function reasonType(item: BoardItem) {
  if (item.stage === 'owner') return 'primary'
  if (item.stage === 'stalled') return 'danger'
  if (item.reason === 'nudge_due') return 'warning'
  if (item.stage === 'working') return 'success'
  return 'info'
}
/** 补充、改期、关闭：和「待跟进」共用一个对话框，做完重新取总表。 */
const dialog = ref<InstanceType<typeof FollowupActionDialog> | null>(null)
const act = (action: FollowupAction, item: BoardItem) => dialog.value?.open(action, item)
onMounted(load)
defineExpose({ load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !data"
      :error="error"
      @retry="load"
    />
    <template v-if="data">
      <!-- 各组件数一眼看完，点一下跳到那一组。 -->
      <div
        class="ma-summary"
        data-test="board-counts"
      >
        <button
          v-for="g in GROUPS"
          :key="g"
          type="button"
          class="ma-chip"
          :class="chipClass(g)"
          :data-test="'jump-' + g"
          @click="jump(g)"
        >
          <span class="ma-chip__n">{{ count(g) }}</span>
          <span>{{ t('assistant.board.groups.' + g) }}</span>
        </button>
        <el-button
          link
          type="primary"
          class="ma-guide-btn"
          data-test="open-guide"
          @click="guideOpen = true"
        >
          <el-icon><QuestionFilled /></el-icon>
          <span>{{ t('assistant.board.guide.title') }}</span>
        </el-button>
      </div>
      <section
        v-for="g in GROUPS"
        :key="g"
        :ref="(el) => { if (el) sections[g] = el as HTMLElement }"
        class="ma-group"
        :class="{ 'is-empty': isEmpty(g) }"
        :data-test="'group-' + g"
      >
        <header class="ma-group__head">
          <h3>{{ t('assistant.board.groups.' + g) }}</h3>
          <span
            class="ma-count"
            :class="chipClass(g)"
          >{{ count(g) }}</span>
          <el-popover
            trigger="click"
            placement="bottom-start"
            :width="320"
          >
            <template #reference>
              <button
                type="button"
                class="ma-help"
                :aria-label="t('assistant.board.guide.about', { name: t('assistant.board.groups.' + g) })"
                :data-test="'help-' + g"
              >
                <el-icon><QuestionFilled /></el-icon>
              </button>
            </template>
            <dl
              class="ma-def"
              :data-test="'help-body-' + g"
            >
              <dt>{{ t('assistant.board.guide.what') }}</dt>
              <dd>{{ t('assistant.board.help.' + g + '.what') }}</dd>
              <dt>{{ t('assistant.board.guide.when') }}</dt>
              <dd>{{ t('assistant.board.help.' + g + '.when') }}</dd>
              <dt>{{ t('assistant.board.guide.todo') }}</dt>
              <dd>{{ t('assistant.board.help.' + g + '.todo') }}</dd>
            </dl>
          </el-popover>
          <span class="ma-group__lead">{{ t('assistant.board.help.' + g + '.short') }}</span>
          <span
            v-if="isEmpty(g)"
            class="ma-empty"
          >{{ t('assistant.board.empty.' + g) }}</span>
        </header>
        <template v-if="g === 'owner' && data.approvals.length">
          <h4 class="ma-subhead">
            {{ t('assistant.board.approvals', { n: data.approvals_total }) }}
          </h4>
          <ul class="ma-list">
            <li
              v-for="a in data.approvals"
              :key="a.id"
              class="ma-row"
              :data-test="'approval-' + a.id"
            >
              <div class="ma-row__main">
                <span class="ma-title">
                  {{ a.summary ?? t('assistant.page.lockedText') }}
                  <el-tag
                    v-if="a.tool === DISPATCH_TOOL"
                    size="small"
                    type="warning"
                  >
                    {{ t('assistant.attention.dispatch') }}
                  </el-tag>
                  <el-tag
                    v-if="PROXIED_TOOLS[a.tool]"
                    size="small"
                    type="warning"
                  >
                    {{ t('assistant.attention.' + PROXIED_TOOLS[a.tool]) }}
                  </el-tag>
                </span>
                <span class="ma-actions">
                  <!-- 链接只可能是部署配置的审批系统入口（后端只放行 http/https）；新窗口打开，不带来源页。 -->
                  <el-button
                    v-if="a.link"
                    tag="a"
                    :href="a.link"
                    target="_blank"
                    rel="noopener noreferrer"
                    size="small"
                    type="primary"
                    plain
                  >{{ t('assistant.attention.open') }}</el-button>
                  <span
                    v-else
                    class="ma-meta"
                  >{{ t('assistant.attention.noLink') }}</span>
                </span>
              </div>
              <div class="ma-meta">
                <span
                  v-if="(a.items ?? 1) > 1"
                >{{ t('assistant.board.batchItems', { n: a.items }) }}</span>
                <span>{{ t('assistant.attention.submittedAt', { time: formatDateTime(a.created_at) }) }}</span>
              </div>
            </li>
          </ul>
          <p
            v-if="data.approvals_total > data.approvals.length"
            class="ma-row ma-meta"
            data-test="approvals-more"
          >
            {{ t('assistant.board.moreApprovals', { n: data.approvals_total - data.approvals.length }) }}
          </p>
        </template>
        <template v-if="gapsIn(g).length">
          <h4 class="ma-subhead">
            {{ t('assistant.board.gapsHeading.' + g, { n: gapsIn(g).length }) }}
          </h4>
          <ul class="ma-list">
            <li
              v-for="gap in gapsIn(g)"
              :key="gap.id"
              class="ma-row"
              :data-test="'gap-' + gap.id"
            >
              <div class="ma-row__main">
                <span class="ma-id">{{ gap.id }}</span>
                <span class="ma-title">
                  {{ t('assistant.board.gap.kinds.' + gap.kind) }} {{ gap.key }}
                  <el-tag
                    v-if="gap.kind === 'business_system'"
                    size="small"
                    type="info"
                    data-test="gap-level"
                  >{{ t('assistant.board.gap.needLevel', { value: level(gap) }) }}</el-tag>
                </span>
                <span class="ma-actions">
                  <el-button
                    v-if="gap.fix === 'self_grant' && gap.status === 'open'"
                    size="small"
                    type="primary"
                    data-test="gap-grant"
                    @click="grant(gap)"
                  >
                    {{ t('assistant.board.gap.grant') }}
                  </el-button>
                  <el-button
                    v-if="gap.fix === 'request' && gap.status === 'open'"
                    size="small"
                    type="primary"
                    data-test="gap-request"
                    @click="request(gap)"
                  >
                    {{ t('assistant.board.gap.request') }}
                  </el-button>
                  <el-button
                    v-if="gap.status === 'requested'"
                    size="small"
                    data-test="gap-withdraw"
                    @click="withdraw(gap)"
                  >
                    {{ t('assistant.board.gap.withdraw') }}
                  </el-button>
                  <el-button
                    size="small"
                    link
                    data-test="gap-decline"
                    @click="decline(gap)"
                  >
                    {{ t('assistant.board.gap.decline') }}
                  </el-button>
                </span>
              </div>
              <div
                v-if="gap.locked || gap.need"
                class="ma-detail"
              >
                {{ gap.locked ? t('assistant.page.lockedText') : gap.need }}
              </div>
              <div class="ma-meta">
                <span>{{ gap.followups.length ? t('assistant.board.gap.blocks', { value: gap.followups.join('、') }) : t('assistant.board.gap.none') }}</span>
              </div>
              <div
                v-if="gap.status === 'requested'"
                class="ma-callout"
                data-test="gap-requested"
              >
                {{ t('assistant.board.gap.requested') }}
              </div>
              <div
                v-else
                class="ma-callout"
              >
                {{ t('assistant.board.gap.fixes.' + gap.fix, { name: gap.id }) }}<span v-if="gap.fix === 'admin' || gap.fix === 'node_admin'">：{{ gap.key }}</span>
              </div>
              <div
                v-if="gap.request?.status === 'rejected'"
                class="ma-callout is-danger"
                data-test="gap-rejected"
              >
                {{ gap.request.decision_note ? t('assistant.board.gap.rejected', { value: gap.request.decision_note }) : t('assistant.board.gap.rejectedPlain') }}
              </div>
            </li>
          </ul>
        </template>
        <h4
          v-if="data.groups[g].length && (gapsIn(g).length || (g === 'owner' && data.approvals.length))"
          class="ma-subhead"
        >
          {{ t('assistant.board.decisions.' + g, { n: data.counts[g] }) }}
        </h4>
        <ul
          v-if="data.groups[g].length"
          class="ma-list"
        >
          <li
            v-for="item in data.groups[g]"
            :key="item.id"
            class="ma-row"
            :data-test="'board-' + item.id"
          >
            <div class="ma-row__main">
              <span class="ma-id">{{ item.id }}</span>
              <span class="ma-title">
                {{ item.locked ? t('assistant.page.lockedText') : item.title }}
                <el-tooltip
                  :content="t('assistant.board.reasonTips.' + item.reason)"
                  placement="top"
                >
                  <el-tag
                    size="small"
                    :type="reasonType(item)"
                    :data-test="'reason-' + item.id"
                  >
                    {{ t('assistant.board.reasons.' + item.reason) }}
                  </el-tag>
                </el-tooltip>
              </span>
              <span class="ma-actions">
                <el-button
                  link
                  type="primary"
                  size="small"
                  :data-test="'supplement-' + item.id"
                  @click="act('supplement', item)"
                >
                  {{ t('assistant.actions.supplement.button') }}
                </el-button>
                <el-button
                  link
                  size="small"
                  :data-test="'reschedule-' + item.id"
                  @click="act('reschedule', item)"
                >
                  {{ t('assistant.actions.reschedule.button') }}
                </el-button>
                <el-button
                  link
                  size="small"
                  :data-test="'close-' + item.id"
                  @click="act('close', item)"
                >
                  {{ t('assistant.actions.close.button') }}
                </el-button>
              </span>
            </div>
            <div class="ma-meta">
              <span>{{ handler(item) }}</span>
              <span
                v-if="item.nudges > 0"
              >{{ t('assistant.board.nudged', { n: item.nudges }) }}</span>
              <span
                v-if="item.idle_days > 0"
                :class="{ 'ma-warn': item.idle_days >= 3 }"
              >{{ t('assistant.board.idle', { n: item.idle_days }) }}</span>
              <span
                v-if="item.due_on"
                :class="{ 'ma-danger': overdue(item) }"
              >{{ t('assistant.board.dueOn', { time: item.due_on }) }}</span>
              <span
                v-if="g === 'scheduled' && item.review_on"
                class="ma-start"
              >{{ t('assistant.board.startOn', { time: item.review_on }) }}</span>
            </div>
            <div
              v-if="item.need"
              class="ma-callout is-primary"
              :data-test="'need-' + item.id"
            >
              {{ t('assistant.board.need', { value: item.need }) }}
            </div>
            <div
              v-if="item.suggestion"
              class="ma-callout"
            >
              {{ t('assistant.board.suggestion', { value: item.suggestion }) }}
            </div>
            <div
              v-for="a in item.approvals"
              :key="a.id"
              class="ma-callout is-warning"
            >
              {{ t('assistant.board.linkedApproval', { value: a.summary ?? t('assistant.page.lockedText') }) }}
              <template v-if="(a.items ?? 1) > 1">
                {{ t('assistant.board.batchItems', { n: a.items }) }}
              </template>
              <a
                v-if="a.link"
                :href="a.link"
                target="_blank"
                rel="noopener noreferrer"
              >{{ t('assistant.attention.open') }}</a>
            </div>
            <div
              v-if="item.job?.blocked_reason"
              class="ma-callout is-danger"
            >
              {{ t('assistant.attention.blockedReason') }}：{{ item.job.blocked_reason }}
            </div>
          </li>
        </ul>
      </section>
    </template>
    <FollowupActionDialog
      ref="dialog"
      :today="data?.today ?? ''"
      @done="load"
    />
    <el-dialog
      v-model="guideOpen"
      :title="t('assistant.board.guide.title')"
      width="min(640px, calc(100vw - 32px))"
      append-to-body
      data-test="guide"
    >
      <p class="ma-guide-intro">
        {{ t('assistant.board.guide.intro') }}
      </p>
      <section
        v-for="g in GROUPS"
        :key="g"
        class="ma-guide-item"
      >
        <h4>{{ t('assistant.board.groups.' + g) }} <small>{{ t('assistant.board.help.' + g + '.short') }}</small></h4>
        <dl class="ma-def">
          <dt>{{ t('assistant.board.guide.what') }}</dt>
          <dd>{{ t('assistant.board.help.' + g + '.what') }}</dd>
          <dt>{{ t('assistant.board.guide.when') }}</dt>
          <dd>{{ t('assistant.board.help.' + g + '.when') }}</dd>
          <dt>{{ t('assistant.board.guide.todo') }}</dt>
          <dd>{{ t('assistant.board.help.' + g + '.todo') }}</dd>
        </dl>
      </section>
      <h4 class="ma-guide-flow">
        {{ t('assistant.board.guide.flowTitle') }}
      </h4>
      <ul class="ma-guide-list">
        <li
          v-for="n in 5"
          :key="n"
        >
          {{ t('assistant.board.guide.flow' + n) }}
        </li>
      </ul>
      <h4 class="ma-guide-flow">
        {{ t('assistant.board.guide.feedbackTitle') }}
      </h4>
      <p class="ma-guide-intro">
        {{ t('assistant.board.guide.feedback') }}
      </p>
    </el-dialog>
  </section>
</template>
<style scoped>
.ma-guide-btn { margin-left: auto; font-size: 13px; }
.ma-guide-btn .el-icon { margin-right: 4px; }
.ma-help { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--el-text-color-secondary); cursor: pointer; }
.ma-help:hover, .ma-help:focus-visible { color: var(--el-color-primary); background: var(--el-fill-color); outline: none; }
.ma-group__lead { font-size: 12px; color: var(--el-text-color-secondary); }
.ma-def { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 6px 10px; font-size: 13px; line-height: 1.6; }
.ma-def dt { color: var(--el-text-color-secondary); white-space: nowrap; }
.ma-def dd { margin: 0; color: var(--el-text-color-regular); }
.ma-guide-intro { margin: 0 0 12px; font-size: 13px; line-height: 1.7; color: var(--el-text-color-regular); }
.ma-guide-item { padding: 10px 0; border-top: 1px solid var(--el-border-color-lighter); }
.ma-guide-item h4 { margin: 0 0 6px; font-size: 14px; }
.ma-guide-item small { margin-left: 6px; font-weight: 400; color: var(--el-text-color-secondary); }
.ma-guide-flow { margin: 14px 0 6px; font-size: 14px; }
.ma-guide-list { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.8; }
.ma-start { color: var(--el-color-primary); }
</style>
