<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import ChatLogDetailDrawer from '@/components/chatLogs/ChatLogDetailDrawer.vue'
import LoadState from '@/components/LoadState.vue'
import { ElMessage } from 'element-plus'
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots as botsApi, chatLogs } from '@/api/admin'
import type { BotOut, ChatLogOut, ChatLogStats } from '@/api/types'
import { usePaged } from '@/composables/usePaged'
import { latencyOf, statusType, tokensOf, userOf } from '@/utils/chatLogs'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()

// 与 coreman/core/db/models/logs.py 的 CHAT_LOG_STATUSES / CHAT_TYPES 一一对应。
const STATUSES = ['success', 'error', 'timeout', 'stopped', 'ask_user', 'failed'] as const
const CHAT_TYPES = ['single', 'group', 'cron'] as const

type ChatFilters = {
  bot_id: string | null
  user: string
  status: string
  chat_type: string
  keyword: string
  since: string | null
  until: string | null
}

/** 空筛选项不进 query（同 AuditLogsView）：`?status=` 发过去没有意义。 */
function clean(q: Record<string, unknown>): Record<string, string | number | boolean> {
  return Object.fromEntries(
    Object.entries(q).filter(([, v]) => v !== '' && v !== null && v !== undefined),
  ) as Record<string, string | number | boolean>
}

const paged = usePaged<ChatLogOut, ChatFilters>((q) => chatLogs.list(clean(q)), {
  bot_id: null,
  user: '',
  status: '',
  chat_type: '',
  keyword: '',
  since: null,
  until: null,
})

const botList = ref<BotOut[]>([])
const tab = ref<'logs' | 'stats'>('logs')
const stats = ref<ChatLogStats | null>(null)
const statsLoading = ref(false)
const detailDrawer = ref<InstanceType<typeof ChatLogDetailDrawer>>()

// el-date-picker 的 v-model 是 Date[]，后端要 ISO 字符串，中间过一层。
const range = ref<[Date, Date] | null>(paged.filters.since && paged.filters.until ? [new Date(paged.filters.since), new Date(paged.filters.until)] : null)
watch(() => [paged.filters.since, paged.filters.until], ([since, until]) => {
  const next = since && until && Number.isFinite(Date.parse(since)) && Number.isFinite(Date.parse(until)) ? [new Date(since), new Date(until)] as [Date, Date] : null
  if (JSON.stringify(range.value) !== JSON.stringify(next)) range.value = next
})
watch(range, (v) => {
  paged.filters.since = v?.[0] ? v[0].toISOString() : null
  paged.filters.until = v?.[1] ? v[1].toISOString() : null
})

function fail(e: unknown) {
  ElMessage.error(errorMessage(e))
}


// el-table-column 会先拿 `{ row: {} }` 空跑一次 default 插槽来探测子列，插槽里直接 t() 动态键
// 会因此收到 undefined 并打印「Not found key」。列上的翻译一律过这两个守卫。
function statusLabel(status: string | undefined): string {
  return status ? t(`chatLogs.statuses.${status}`) : '—'
}

function chatTypeLabel(chatType: string | undefined): string {
  return chatType ? t(`chatLogs.chatTypes.${chatType}`) : '—'
}


async function loadStats(): Promise<void> {
  statsLoading.value = true
  try {
    stats.value = await chatLogs.stats(clean(paged.filters))
  } catch (e) {
    fail(e)
  } finally {
    statsLoading.value = false
  }
}

/** 统计只在「按机器人统计」页签被激活时才拉，切回列表不重复请求。 */
function onTabChange(name: string | number): void {
  if (name === 'stats') loadStats()
}

async function openDetail(id: number): Promise<void> {
  await detailDrawer.value?.open(id)
}

function onRowClick(row: ChatLogOut): void {
  openDetail(row.id)
}

function search() {
  paged.page.value = 1
  paged.load()
  if (tab.value === 'stats') loadStats()
}

function resetFilters() {
  paged.reset()
  range.value = null
  paged.load()
}

function onPageChange(p: number) {
  paged.page.value = p
  paged.load()
}

function onSizeChange(size: number) {
  paged.perPage.value = size
  paged.page.value = 1
  paged.load()
}

onMounted(async () => {
  paged.load()
  try {
    // scope=all：对话记录本身已按可见性过滤，下拉里列出用户能看到的全部机器人即可。
    botList.value = (await botsApi.list({ scope: 'all', per_page: 200 })).items
  } catch (e) {
    fail(e)
  }
})

defineExpose({ paged, openDetail })
</script>

<template>
  <div class="chat-logs-view">
    <div class="page-header cm-page-header">
      <h2>{{ t('chatLogs.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.chat-logs') }}
      </p>
    </div>

    <el-form
      :inline="true"
      @submit.prevent
    >
      <el-form-item
        :label="t('chatLogs.bot')"
        data-test="filter-bot"
      >
        <el-select
          v-model="paged.filters.bot_id"
          filterable
          clearable
          style="width: 200px"
        >
          <el-option
            v-for="b in botList"
            :key="b.id"
            :label="b.name"
            :value="b.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item
        :label="t('chatLogs.status')"
        data-test="filter-status"
      >
        <el-select
          v-model="paged.filters.status"
          clearable
          style="width: 150px"
        >
          <el-option
            v-for="s in STATUSES"
            :key="s"
            :label="t(`chatLogs.statuses.${s}`)"
            :value="s"
          />
        </el-select>
      </el-form-item>
      <el-form-item
        :label="t('chatLogs.chatType')"
        data-test="filter-chat-type"
      >
        <el-select
          v-model="paged.filters.chat_type"
          clearable
          style="width: 150px"
        >
          <el-option
            v-for="c in CHAT_TYPES"
            :key="c"
            :label="t(`chatLogs.chatTypes.${c}`)"
            :value="c"
          />
        </el-select>
      </el-form-item>
      <el-form-item
        :label="t('chatLogs.user')"
        data-test="filter-user"
      >
        <el-input
          v-model="paged.filters.user"
          clearable
          style="width: 160px"
        />
      </el-form-item>
      <el-form-item
        :label="t('chatLogs.keyword')"
        data-test="filter-keyword"
      >
        <el-input
          v-model="paged.filters.keyword"
          clearable
          maxlength="200"
          style="width: 200px"
        />
      </el-form-item>
      <el-form-item
        :label="t('chatLogs.timeRange')"
        data-test="filter-range"
      >
        <el-date-picker
          v-model="range"
          type="datetimerange"
          unlink-panels
          :start-placeholder="t('audit.since')"
          :end-placeholder="t('audit.until')"
        />
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          data-test="search"
          @click="search"
        >
          {{ t('common.search') }}
        </el-button>
        <el-button
          data-test="reset"
          @click="resetFilters"
        >
          {{ t('common.reset') }}
        </el-button>
      </el-form-item>
    </el-form>

    <el-tabs
      v-model="tab"
      @tab-change="onTabChange"
    >
      <el-tab-pane
        :label="t('chatLogs.title')"
        name="logs"
      >
        <LoadState
          :error="paged.error.value"
          @retry="paged.load"
        />
        <el-table
          v-loading="paged.loading.value"
          :data="paged.items.value"
          row-key="id"
          @row-click="onRowClick"
        >
          <el-table-column
            :label="t('chatLogs.time')"
            width="180"
          >
            <template #default="{ row }: { row: ChatLogOut }">
              {{ formatDateTime(row.request_at) }}
            </template>
          </el-table-column>
          <el-table-column
            prop="bot_name"
            :label="t('chatLogs.bot')"
            width="150"
          />
          <el-table-column
            :label="t('chatLogs.user')"
            width="140"
          >
            <template #default="{ row }: { row: ChatLogOut }">
              {{ userOf(row) }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('chatLogs.chatType')"
            width="100"
          >
            <template #default="{ row }: { row: ChatLogOut }">
              {{ chatTypeLabel(row.chat_type) }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('chatLogs.message')"
            min-width="220"
          >
            <template #default="{ row }: { row: ChatLogOut }">
              <span class="preview">{{ row.message_preview ?? '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('chatLogs.status')"
            width="110"
          >
            <template #default="{ row }: { row: ChatLogOut }">
              <el-tag
                :type="statusType(row.status)"
                disable-transitions
              >
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('chatLogs.latency')"
            width="110"
          >
            <template #default="{ row }: { row: ChatLogOut }">
              {{ latencyOf(row.latency_ms) }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('chatLogs.tokens')"
            width="120"
          >
            <template #default="{ row }: { row: ChatLogOut }">
              {{ tokensOf(row) }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('common.actions')"
            width="100"
          >
            <template #default="{ row }: { row: ChatLogOut }">
              <el-button
                link
                type="primary"
                :data-test="'row-' + row.id"
                @click.stop="openDetail(row.id)"
              >
                {{ t('chatLogs.detail') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-pagination
          :total="paged.total.value"
          :current-page="paged.page.value"
          :page-size="paged.perPage.value"
          :page-sizes="[20, 50, 100, 200]"
          layout="total, prev, pager, next, sizes"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </el-tab-pane>

      <el-tab-pane
        :label="t('chatLogs.stats')"
        name="stats"
        data-test="tab-stats"
      >
        <div v-loading="statsLoading">
          <el-descriptions
            v-if="stats"
            :column="2"
            border
          >
            <el-descriptions-item :label="t('chatLogs.total')">
              {{ stats.total }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('chatLogs.avgLatency')">
              {{ latencyOf(stats.avg_latency_ms) }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('chatLogs.byStatus')">
              <el-tag
                v-for="(count, s) in stats.by_status"
                :key="s"
                :type="statusType(String(s))"
                class="chip"
                disable-transitions
              >
                {{ t(`chatLogs.statuses.${s}`) }} {{ count }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item :label="t('chatLogs.tokens')">
              {{ stats.tokens.input }} / {{ stats.tokens.output }}
              （cache {{ stats.tokens.cache_read }} / {{ stats.tokens.cache_creation }}）
            </el-descriptions-item>
          </el-descriptions>

          <h4>{{ t('chatLogs.byBot') }}</h4>
          <el-table :data="stats?.by_bot ?? []">
            <el-table-column
              min-width="140"
              prop="bot_name"
              :label="t('chatLogs.bot')"
            />
            <el-table-column
              prop="total"
              :label="t('chatLogs.total')"
              width="120"
            />
            <el-table-column
              prop="success"
              :label="t('chatLogs.statuses.success')"
              width="120"
            />
            <el-table-column
              prop="error"
              :label="t('chatLogs.statuses.error')"
              width="120"
            />
            <el-table-column
              :label="t('chatLogs.avgLatency')"
              width="140"
            >
              <template #default="{ row }: { row: ChatLogStats['by_bot'][number] }">
                {{ latencyOf(row.avg_latency_ms) }}
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <ChatLogDetailDrawer ref="detailDrawer" />
  </div>
</template>

<style scoped>
.preview { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip { margin-right: 6px; }
</style>
