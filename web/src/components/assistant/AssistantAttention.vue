<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { DISPATCH_TOOL, myAssistant, type Attention } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const data = ref<Attention | null>(null)
const error = ref(''), loading = ref(false)
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.attention() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
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
      <h3>{{ t('assistant.attention.approvals') }}</h3>
      <p
        v-if="!data.approvals.length"
        class="muted"
      >
        {{ t('assistant.attention.approvalsEmpty') }}
      </p>
      <ul v-else>
        <li
          v-for="item in data.approvals"
          :key="item.id"
          :data-test="'approval-' + item.id"
        >
          <strong>{{ item.summary ?? t('assistant.page.lockedText') }}</strong>
          <el-tag
            v-if="item.tool === DISPATCH_TOOL"
            size="small"
            type="warning"
          >
            {{ t('assistant.attention.dispatch') }}
          </el-tag>
          <span class="muted">{{ item.tool }} · {{ t('assistant.attention.submittedAt', { time: formatDateTime(item.created_at) }) }}</span>
          <!-- 链接只可能是部署配置的审批系统入口（后端只放行 http/https）；新窗口打开，不带来源页。 -->
          <a
            v-if="item.link"
            :href="item.link"
            target="_blank"
            rel="noopener noreferrer"
          >{{ t('assistant.attention.open') }}</a>
          <span
            v-else
            class="muted"
          >{{ t('assistant.attention.noLink') }}</span>
        </li>
      </ul>
      <h3>{{ t('assistant.attention.jobs') }}</h3>
      <p
        v-if="!data.jobs.length"
        class="muted"
      >
        {{ t('assistant.attention.jobsEmpty') }}
      </p>
      <ul v-else>
        <li
          v-for="job in data.jobs"
          :key="job.id"
          :data-test="'blocked-' + job.id"
        >
          <strong>{{ job.locked ? t('assistant.page.lockedText') : job.title }}</strong>
          <span v-if="job.blocked_reason">{{ t('assistant.attention.blockedReason') }}：{{ job.blocked_reason }}</span>
        </li>
      </ul>
      <h3>{{ t('assistant.attention.followups') }}</h3>
      <p
        v-if="!data.followups.length"
        class="muted"
      >
        {{ t('assistant.attention.followupsEmpty') }}
      </p>
      <ul v-else>
        <li
          v-for="item in data.followups"
          :key="item.id"
          :data-test="'due-' + item.id"
        >
          <code>{{ item.id }}</code>
          <strong>{{ item.locked ? t('assistant.page.lockedText') : item.title }}</strong>
          <el-tag size="small">
            {{ t('assistant.followups.categories.' + item.category) }}
          </el-tag>
        </li>
      </ul>
    </template>
  </section>
</template>
<style scoped>
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.muted { color: var(--el-text-color-secondary); }
</style>
