<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { DISPATCH_TOOL, PROXIED_TOOLS, myAssistant, type SettledApproval } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const items = ref<SettledApproval[] | null>(null)
const error = ref(''), loading = ref(false)
// 全部终态各有明确的状态文案：已执行、被拒、撤回、失败、过期、作废。
const tags: Record<SettledApproval['status'], 'success' | 'danger' | 'info' | 'warning'> = {
  executed: 'success', rejected: 'danger', withdrawn: 'info', failed: 'danger', expired: 'warning', stale: 'info',
}
async function load() {
  loading.value = true; error.value = ''
  try { items.value = (await myAssistant.recentApprovals()).items }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
onMounted(load)
defineExpose({ load })
</script>
<template>
  <section class="assistant-section">
    <LoadState
      :loading="loading && !items"
      :error="error"
      @retry="load"
    />
    <section
      v-if="items"
      class="ma-group"
      :class="{ 'is-empty': !items.length }"
    >
      <header class="ma-group__head">
        <h3>{{ t('assistant.page.tabs.recent') }}</h3>
        <span class="ma-count">{{ items.length }}</span>
        <span
          v-if="!items.length"
          class="ma-empty"
        >{{ t('assistant.recent.empty') }}</span>
      </header>
      <ul
        v-if="items.length"
        class="ma-list"
      >
        <li
          v-for="item in items"
          :key="item.id"
          class="ma-row"
          :data-test="'settled-' + item.id"
        >
          <div class="ma-row__main">
            <el-tag
              size="small"
              :type="tags[item.status]"
              class="recent-status"
            >
              {{ t('assistant.recent.statuses.' + item.status) }}
            </el-tag>
            <!-- 工具名是给排查用的：鼠标停在标题上才显示。 -->
            <span
              class="ma-title"
              :title="item.tool"
            >
              {{ item.summary ?? t('assistant.page.lockedText') }}
              <el-tag
                v-if="item.tool === DISPATCH_TOOL"
                size="small"
                type="warning"
              >
                {{ t('assistant.recent.dispatch') }}
              </el-tag>
              <el-tag
                v-if="PROXIED_TOOLS[item.tool]"
                size="small"
                type="warning"
              >
                {{ t('assistant.recent.' + PROXIED_TOOLS[item.tool]) }}
              </el-tag>
            </span>
          </div>
          <div class="ma-meta">
            <span v-if="item.executed_at">{{ t('assistant.recent.executedAt', { time: formatDateTime(item.executed_at) }) }}</span>
            <span v-else-if="item.decided_at">{{ t('assistant.recent.decidedAt', { time: formatDateTime(item.decided_at) }) }}</span>
            <span
              v-if="item.error"
              class="ma-danger"
            >{{ t('assistant.recent.error', { detail: item.error }) }}</span>
          </div>
          <div
            v-if="item.reason"
            class="ma-callout"
            :class="{ 'is-danger': item.status === 'rejected' }"
          >
            {{ t('assistant.recent.reason') }}：{{ item.reason }}
          </div>
        </li>
      </ul>
    </section>
  </section>
</template>
<style scoped>
.recent-status { flex-shrink: 0; margin-top: 2px; }
</style>
