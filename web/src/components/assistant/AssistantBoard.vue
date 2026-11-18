<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { DISPATCH_TOOL, PROXIED_TOOLS, myAssistant, type Board, type BoardGroup, type BoardItem } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const GROUPS: BoardGroup[] = ['owner', 'stalled', 'working', 'others', 'scheduled']
const data = ref<Board | null>(null)
const error = ref(''), loading = ref(false)
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.board() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
function handler(item: BoardItem) {
  return t('assistant.board.handler.' + item.handler, { name: item.handler_name ?? '' })
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
        <h3>{{ t('assistant.board.groups.' + g) }}（{{ data.counts[g] + (g === 'owner' ? data.approvals_total : 0) }}）</h3>
        <template v-if="g === 'owner' && data.approvals.length">
          <h4>{{ t('assistant.board.approvals', { n: data.approvals_total }) }}</h4>
          <ul>
            <li
              v-for="a in data.approvals"
              :key="a.id"
              :data-test="'approval-' + a.id"
            >
              <strong>{{ a.summary ?? t('assistant.page.lockedText') }}</strong>
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
          <h4 v-if="data.groups.owner.length">
            {{ t('assistant.board.decisions', { n: data.counts.owner }) }}
          </h4>
        </template>
        <p
          v-if="!data.groups[g].length && !(g === 'owner' && data.approvals.length)"
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
