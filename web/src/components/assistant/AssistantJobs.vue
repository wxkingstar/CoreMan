<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type AssistantJob, type JobList } from '@/api/myAssistant'
import LoadState from '@/components/LoadState.vue'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const data = ref<JobList | null>(null)
const error = ref(''), loading = ref(false)
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await myAssistant.jobs() }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
/** 取消确认框不算失败。 */
async function cancel(job: AssistantJob) {
  try {
    await ElMessageBox.confirm(t('assistant.jobs.cancelConfirm', { name: job.title || job.id }), t('assistant.jobs.cancel'), { type: 'warning' })
    await myAssistant.cancelJob(job.id)
    ElMessage.success(t('assistant.jobs.cancelled'))
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
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
      <template
        v-for="group in (['active', 'recent'] as const)"
        :key="group"
      >
        <h3>{{ t('assistant.jobs.' + group) }}</h3>
        <p
          v-if="!data[group].length"
          class="muted"
        >
          {{ t('assistant.jobs.' + group + 'Empty') }}
        </p>
        <ul v-else>
          <li
            v-for="job in data[group]"
            :key="job.id"
            :data-test="'job-' + job.id"
          >
            <div class="job-head">
              <strong>{{ job.locked ? t('assistant.page.lockedText') : job.title }}</strong>
              <el-tag size="small">
                {{ t('assistant.jobs.statuses.' + job.status) }}
              </el-tag>
              <!-- 调度公共员工与子任务同表：按 kind 区分，并写明目标员工。 -->
              <el-tag
                v-if="job.kind === 'bot_help'"
                size="small"
                type="warning"
                :data-test="'dispatch-' + job.id"
              >
                {{ t('assistant.jobs.kinds.bot_help') }}
              </el-tag>
              <span
                v-if="job.target"
                class="muted"
              >{{ t('assistant.jobs.target', { name: job.target.name ?? t('assistant.jobs.targetGone') }) }}</span>
              <!-- 平台派的（下班整理）不是所有者让助理做的事，单独标出。 -->
              <el-tag
                v-if="job.origin === 'system'"
                size="small"
                type="info"
                :data-test="'system-' + job.id"
              >
                {{ t('assistant.jobs.system') }}
              </el-tag>
              <span class="muted">{{ t('assistant.jobs.rounds', { n: job.rounds }) }} · {{ t('assistant.jobs.startedAt', { time: formatDateTime(job.created_at) }) }}</span>
              <span
                v-if="job.finished_at"
                class="muted"
              >{{ t('assistant.jobs.finishedAt', { time: formatDateTime(job.finished_at) }) }}</span>
              <span
                v-if="job.error_code"
                class="muted"
              >{{ t('assistant.jobs.errorCode', { detail: job.error_code }) }}</span>
              <el-button
                v-if="group === 'active'"
                link
                type="danger"
                :data-test="'cancel-' + job.id"
                @click="cancel(job)"
              >
                {{ t('assistant.jobs.cancel') }}
              </el-button>
            </div>
            <details v-if="job.brief">
              <summary>{{ t('assistant.jobs.brief') }}</summary>
              <pre>{{ job.brief }}</pre>
            </details>
            <details
              v-if="job.blocked_reason"
              open
            >
              <summary>{{ t('assistant.attention.blockedReason') }}</summary>
              <pre>{{ job.blocked_reason }}</pre>
            </details>
            <details
              v-if="job.result"
              open
            >
              <summary>{{ t('assistant.jobs.result') }}</summary>
              <pre>{{ job.result }}</pre>
            </details>
          </li>
        </ul>
      </template>
    </template>
  </section>
</template>
<style scoped>
.assistant-section ul { list-style: none; padding: 0; }
.assistant-section li { padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.job-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.assistant-section pre { white-space: pre-wrap; word-break: break-word; margin: 4px 0; }
.muted { color: var(--el-text-color-secondary); }
</style>
