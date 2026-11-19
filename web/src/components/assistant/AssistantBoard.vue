<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { DISPATCH_TOOL, PROXIED_TOOLS, myAssistant, type Board, type BoardGap, type BoardGroup, type BoardItem } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { askSnoozeDate } from '@/utils/assistantFollowups'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const GROUPS: BoardGroup[] = ['owner', 'stalled', 'working', 'others', 'gap', 'scheduled']
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
const isEmpty = (g: BoardGroup) => (g === 'gap' ? !data.value?.gaps.length : !data.value?.groups[g].length && !(g === 'owner' && data.value?.approvals.length))
const sections = ref<Partial<Record<BoardGroup, HTMLElement>>>({})
function jump(g: BoardGroup) {
  sections.value[g]?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
/** 期限到了或过了标红，三个工作日以上没进展标黄。 */
const overdue = (item: BoardItem) => !!item.due_on && !!data.value && item.due_on <= data.value.today
/** 在总表上就地办完、推后：和「待跟进」里同一个接口，做完重新取总表。 */
const busy = ref(false)
async function act(action: () => Promise<unknown>, done: string) {
  if (busy.value) return
  busy.value = true
  try { await action(); ElMessage.success(t(done)); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
const finish = (item: BoardItem) => act(() => myAssistant.closeFollowup(item.id, false), 'assistant.followups.closed')
const snooze = (item: BoardItem) => act(async () => {
  const value = await askSnoozeDate(t, data.value?.today ?? '')
  await myAssistant.snoozeFollowup(item.id, value)
}, 'assistant.followups.snoozed')
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
          <h3 v-if="g === 'gap'">
            {{ t('assistant.board.gap.heading', { count: data.counts.gap, n: data.gaps.length }) }}
          </h3>
          <template v-else>
            <h3>{{ t('assistant.board.groups.' + g) }}</h3>
            <span
              class="ma-count"
              :class="chipClass(g)"
            >{{ count(g) }}</span>
          </template>
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
          <h4
            v-if="data.groups.owner.length"
            class="ma-subhead"
          >
            {{ t('assistant.board.decisions', { n: data.counts.owner }) }}
          </h4>
        </template>
        <ul
          v-if="g === 'gap' && data.gaps.length"
          class="ma-list"
        >
          <li
            v-for="gap in data.gaps"
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
        <ul
          v-else-if="g !== 'gap' && data.groups[g].length"
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
                <el-tag
                  v-if="g === 'stalled'"
                  size="small"
                  type="danger"
                >
                  {{ t('assistant.board.stage.' + item.stage) }}
                </el-tag>
              </span>
              <span class="ma-actions">
                <el-button
                  link
                  type="primary"
                  size="small"
                  :disabled="busy"
                  :data-test="'finish-' + item.id"
                  @click="finish(item)"
                >
                  {{ t('assistant.followups.done') }}
                </el-button>
                <el-button
                  link
                  size="small"
                  :disabled="busy"
                  :data-test="'snooze-' + item.id"
                  @click="snooze(item)"
                >
                  {{ t('assistant.followups.snooze') }}
                </el-button>
              </span>
            </div>
            <div class="ma-meta">
              <span>{{ handler(item) }}</span>
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
              >{{ t('assistant.board.reviewOn', { time: item.review_on }) }}</span>
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
  </section>
</template>
