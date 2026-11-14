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
    <p
      v-if="items && !items.length"
      class="muted"
    >
      {{ t('assistant.recent.empty') }}
    </p>
    <ul v-if="items && items.length">
      <li
        v-for="item in items"
        :key="item.id"
        :data-test="'settled-' + item.id"
      >
        <el-tag
          size="small"
          :type="tags[item.status]"
        >
          {{ t('assistant.recent.statuses.' + item.status) }}
        </el-tag>
        <strong>{{ item.summary ?? t('assistant.page.lockedText') }}</strong>
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
        <span class="muted">{{ item.tool }}</span>
        <span
          v-if="item.executed_at"
          class="muted"
        >{{ t('assistant.recent.executedAt', { time: formatDateTime(item.executed_at) }) }}</span>
        <span
          v-else-if="item.decided_at"
          class="muted"
        >{{ t('assistant.recent.decidedAt', { time: formatDateTime(item.decided_at) }) }}</span>
        <span
          v-if="item.error"
          class="muted"
        >{{ t('assistant.recent.error', { detail: item.error }) }}</span>
        <span v-if="item.reason">{{ t('assistant.recent.reason') }}：{{ item.reason }}</span>
      </li>
    </ul>
  </section>
</template>
<style scoped>
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.muted { color: var(--el-text-color-secondary); }
</style>
