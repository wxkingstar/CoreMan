<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { DISPATCH_TOOL, PROXIED_TOOLS, myAssistant, type Board, type BoardGap, type BoardGroup, type BoardItem } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
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
      <p
        class="muted"
        data-test="board-counts"
      >
        <span
          v-for="g in GROUPS"
          :key="g"
        >{{ t('assistant.board.groups.' + g) }} {{ data.counts[g] }}　</span>
      </p>
      <template
        v-for="g in GROUPS"
        :key="g"
      >
        <h3 v-if="g === 'gap'">
          {{ t('assistant.board.gap.heading', { count: data.counts.gap, n: data.gaps.length }) }}
        </h3>
        <h3 v-else>
          {{ t('assistant.board.groups.' + g) }}（{{ data.counts[g] + (g === 'owner' ? data.approvals_total : 0) }}）
        </h3>
        <template v-if="g === 'owner' && data.approvals.length">
          <h4>{{ t('assistant.board.approvals', { n: data.approvals_total }) }}</h4>
          <ul>
            <li
              v-for="a in data.approvals"
              :key="a.id"
              :data-test="'approval-' + a.id"
            >
              <strong>{{ a.summary ?? t('assistant.page.lockedText') }}</strong>
              <span
                v-if="(a.items ?? 1) > 1"
                class="muted"
              >{{ t('assistant.board.batchItems', { n: a.items }) }}</span>
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
              <span class="muted">{{ t('assistant.attention.submittedAt', { time: formatDateTime(a.created_at) }) }}</span>
              <!-- 链接只可能是部署配置的审批系统入口（后端只放行 http/https）；新窗口打开，不带来源页。 -->
              <a
                v-if="a.link"
                :href="a.link"
                target="_blank"
                rel="noopener noreferrer"
              >{{ t('assistant.attention.open') }}</a>
              <span
                v-else
                class="muted"
              >{{ t('assistant.attention.noLink') }}</span>
            </li>
          </ul>
          <p
            v-if="data.approvals_total > data.approvals.length"
            class="muted"
            data-test="approvals-more"
          >
            {{ t('assistant.board.moreApprovals', { n: data.approvals_total - data.approvals.length }) }}
          </p>
          <h4 v-if="data.groups.owner.length">
            {{ t('assistant.board.decisions', { n: data.counts.owner }) }}
          </h4>
        </template>
        <template v-if="g === 'gap'">
          <p
            v-if="!data.gaps.length"
            class="muted"
          >
            {{ t('assistant.board.empty.gap') }}
          </p>
          <ul v-else>
            <li
              v-for="gap in data.gaps"
              :key="gap.id"
              :data-test="'gap-' + gap.id"
            >
              <strong>{{ gap.id }} {{ t('assistant.board.gap.kinds.' + gap.kind) }} {{ gap.key }}</strong>
              <span>{{ gap.locked ? t('assistant.page.lockedText') : gap.need }}</span>
              <span class="muted">{{ gap.followups.length ? t('assistant.board.gap.blocks', { value: gap.followups.join('、') }) : t('assistant.board.gap.none') }}</span>
              <div
                v-if="gap.status === 'requested'"
                class="muted"
                data-test="gap-requested"
              >
                {{ t('assistant.board.gap.requested') }}
              </div>
              <div
                v-else
                class="muted"
              >
                {{ t('assistant.board.gap.fixes.' + gap.fix, { name: gap.id }) }}
                <span v-if="gap.fix === 'admin' || gap.fix === 'node_admin'">：{{ gap.key }}</span>
              </div>
              <div
                v-if="gap.kind === 'business_system'"
                class="muted"
                data-test="gap-level"
              >
                {{ t('assistant.board.gap.needLevel', { value: level(gap) }) }}
              </div>
              <div
                v-if="gap.request?.status === 'rejected'"
                class="muted"
                data-test="gap-rejected"
              >
                {{ gap.request.decision_note ? t('assistant.board.gap.rejected', { value: gap.request.decision_note }) : t('assistant.board.gap.rejectedPlain') }}
              </div>
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
                data-test="gap-decline"
                @click="decline(gap)"
              >
                {{ t('assistant.board.gap.decline') }}
              </el-button>
            </li>
          </ul>
        </template>
        <p
          v-if="g !== 'gap' && !data.groups[g].length && !(g === 'owner' && data.approvals.length)"
          class="muted"
        >
          {{ t('assistant.board.empty.' + g) }}
        </p>
        <ul v-else-if="data.groups[g].length">
          <li
            v-for="item in data.groups[g]"
            :key="item.id"
            :data-test="'board-' + item.id"
          >
            <strong>{{ item.id }} {{ item.locked ? t('assistant.page.lockedText') : item.title }}</strong>
            <el-tag
              v-if="g === 'stalled'"
              size="small"
              type="danger"
            >
              {{ t('assistant.board.stage.' + item.stage) }}
            </el-tag>
            <span class="muted">{{ handler(item) }}</span>
            <span
              v-if="item.idle_days > 0"
              class="muted"
            >· {{ t('assistant.board.idle', { n: item.idle_days }) }}</span>
            <span
              v-if="item.due_on"
              class="muted"
            >· {{ t('assistant.board.dueOn', { time: item.due_on }) }}</span>
            <span
              v-if="g === 'scheduled' && item.review_on"
              class="muted"
            >· {{ t('assistant.board.reviewOn', { time: item.review_on }) }}</span>
            <div
              v-if="item.suggestion"
              class="muted"
            >
              {{ t('assistant.board.suggestion', { value: item.suggestion }) }}
            </div>
            <div
              v-for="a in item.approvals"
              :key="a.id"
              class="muted"
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
              class="muted"
            >
              {{ t('assistant.attention.blockedReason') }}：{{ item.job.blocked_reason }}
            </div>
          </li>
        </ul>
      </template>
    </template>
  </section>
</template>
<style scoped>
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.muted { color: var(--el-text-color-secondary); }
</style>
