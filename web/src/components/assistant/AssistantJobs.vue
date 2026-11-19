<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
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
const STATUS_TAGS: Record<AssistantJob['status'], 'primary' | 'success' | 'info' | 'warning' | 'danger'> = {
  queued: 'info', running: 'primary', done: 'success', failed: 'danger', cancelled: 'info', blocked: 'warning',
}
/** 结论默认只露前几行，点开看全文；进行中的没有结论，交代原文默认收起。 */
const expanded = reactive(new Set<string>())
function toggle(key: string) { if (expanded.has(key)) expanded.delete(key); else expanded.add(key) }
/** 只有长文才需要「展开」：按行数和字数粗略判断。 */
const isLong = (text: string) => text.split('\n').length > 3 || text.length > 240
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
      <section
        v-for="group in (['active', 'recent'] as const)"
        :key="group"
        class="ma-group"
        :class="{ 'is-empty': !data[group].length }"
      >
        <header class="ma-group__head">
          <h3>{{ t('assistant.jobs.' + group) }}</h3>
          <span
            class="ma-count"
            :class="{ 'is-primary': group === 'active' && data[group].length > 0 }"
          >{{ data[group].length }}</span>
          <span
            v-if="!data[group].length"
            class="ma-empty"
          >{{ t('assistant.jobs.' + group + 'Empty') }}</span>
        </header>
        <ul
          v-if="data[group].length"
          class="ma-list"
        >
          <li
            v-for="job in data[group]"
            :key="job.id"
            class="ma-row"
            :data-test="'job-' + job.id"
          >
            <div class="ma-row__main">
              <span class="ma-title">
                {{ job.locked ? t('assistant.page.lockedText') : job.title }}
                <el-tag
                  size="small"
                  :type="STATUS_TAGS[job.status]"
                >
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
                <!-- 平台派的（下班整理）不是所有者让助理做的事，单独标出。 -->
                <el-tag
                  v-if="job.origin === 'system'"
                  size="small"
                  type="info"
                  effect="plain"
                  :data-test="'system-' + job.id"
                >
                  {{ t('assistant.jobs.system') }}
                </el-tag>
              </span>
              <span
                v-if="group === 'active'"
                class="ma-actions"
              >
                <el-button
                  link
                  type="danger"
                  size="small"
                  :data-test="'cancel-' + job.id"
                  @click="cancel(job)"
                >
                  {{ t('assistant.jobs.cancel') }}
                </el-button>
              </span>
            </div>
            <div class="ma-meta">
              <span v-if="job.target">{{ t('assistant.jobs.target', { name: job.target.name ?? t('assistant.jobs.targetGone') }) }}</span>
              <span>{{ t('assistant.jobs.rounds', { n: job.rounds }) }}</span>
              <span>{{ t('assistant.jobs.startedAt', { time: formatDateTime(job.created_at) }) }}</span>
              <span v-if="job.finished_at">{{ t('assistant.jobs.finishedAt', { time: formatDateTime(job.finished_at) }) }}</span>
              <span
                v-if="job.error_code"
                class="ma-danger"
              >{{ t('assistant.jobs.errorCode', { detail: job.error_code }) }}</span>
            </div>
            <div
              v-if="job.blocked_reason"
              class="ma-callout is-warning"
            >
              <div class="ma-block__label">
                {{ t('assistant.attention.blockedReason') }}
              </div>
              <pre class="ma-text">{{ job.blocked_reason }}</pre>
            </div>
            <div
              v-if="job.result"
              class="ma-block"
            >
              <div class="ma-block__label">
                {{ t('assistant.jobs.result') }}
              </div>
              <!-- 正文按纯文本显示：内容可能来自别人发来的消息，不当成 HTML 或 Markdown 渲染。 -->
              <pre
                class="ma-text"
                :class="{ 'ma-clamp': isLong(job.result) && !expanded.has('r' + job.id) }"
              >{{ job.result }}</pre>
              <button
                v-if="isLong(job.result)"
                type="button"
                class="ma-toggle"
                :data-test="'toggle-result-' + job.id"
                @click="toggle('r' + job.id)"
              >
                {{ expanded.has('r' + job.id) ? t('assistant.page.collapse') : t('assistant.page.expand') }}
              </button>
            </div>
            <div
              v-if="job.brief"
              class="ma-block"
            >
              <!-- 进行中的看交代了什么；结束了的以结论为主，交代原文收起。 -->
              <div
                v-if="group === 'active'"
                class="ma-block__label"
              >
                {{ t('assistant.jobs.brief') }}
              </div>
              <button
                v-else
                type="button"
                class="ma-toggle"
                :aria-expanded="expanded.has('b' + job.id)"
                :data-test="'toggle-brief-' + job.id"
                @click="toggle('b' + job.id)"
              >
                {{ expanded.has('b' + job.id) ? '▾' : '▸' }} {{ t('assistant.jobs.brief') }}
              </button>
              <pre
                v-show="group === 'active' || expanded.has('b' + job.id)"
                class="ma-text ma-callout"
              >{{ job.brief }}</pre>
            </div>
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>
