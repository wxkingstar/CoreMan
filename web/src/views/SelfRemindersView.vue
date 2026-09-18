<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { selfReminders, type SelfItem, type SelfSchedule } from '@/api/selfReminders'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'
const { t, te, locale } = useI18n()
const rows = ref<SelfItem[]>([])
const error = ref('')
const busy = ref(false)
async function load() {
  busy.value = true
  try { rows.value = await selfReminders.list(); error.value = '' }
  catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
/** 操作失败（例如恢复时超过启用上限）直接提示后端原话；取消确认框不算失败。 */
async function act(action: () => Promise<unknown>, done?: string) {
  busy.value = true
  try { await action(); if (done) ElMessage.success(t(done)); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
const cancel = (id: string) => act(() => selfReminders.cancel(id))
const pause = (row: SelfSchedule) => act(() => selfReminders.pause(row.id), 'selfReminders.pausedDone')
const resume = (row: SelfSchedule) => act(() => selfReminders.resume(row.id), 'selfReminders.resumedDone')
const remove = (row: SelfSchedule) => act(async () => {
  await ElMessageBox.confirm(t('selfReminders.confirmDelete', { name: row.name }), t('selfReminders.delete'), { type: 'warning' })
  await selfReminders.remove(row.id)
}, 'selfReminders.deletedDone')
function time(value: string | null | undefined, timeZone = 'Asia/Shanghai') {
  if (!value || Number.isNaN(new Date(value).getTime())) return '—'
  const zone = timeZone === 'Asia/Shanghai' ? 'UTC+08:00' : timeZone
  try { return new Date(value).toLocaleString(locale.value, { timeZone, hour12: false }) + ` (${zone})` }
  catch { return formatDateTime(value) + ' (UTC+08:00)' }
}
/** 没有翻译的状态原样显示，不让界面出现 i18n 键名。 */
function label(prefix: string, status: string) {
  const key = `${prefix}.${status}`
  return te(key) ? t(key) : status
}
function statusTag(row: SelfSchedule) {
  if (row.running) return { type: 'warning' as const, text: t('selfReminders.state.running') }
  return row.enabled
    ? { type: 'success' as const, text: t('selfReminders.state.active') }
    : { type: 'info' as const, text: t('selfReminders.state.paused') }
}
onMounted(load)
</script>
<template>
  <section class="reminders">
    <h1>{{ t('menu.selfReminders') }}</h1>
    <p>{{ t('selfReminders.hint') }}</p>
    <p>{{ t('selfReminders.scheduleHint') }}</p>
    <p>{{ t('selfReminders.deliveryHint') }}</p>
    <el-button
      :loading="busy"
      @click="load"
    >
      {{ t('selfReminders.refresh') }}
    </el-button>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
    />
    <el-empty
      v-if="!busy && !rows.length"
      :description="t('selfReminders.empty')"
    />
    <template
      v-for="row in rows"
      :key="row.id"
    >
      <article
        v-if="row.type === 'schedule'"
        class="reminder schedule"
        :data-test="'schedule-' + row.id"
      >
        <header class="schedule-heading">
          <div>
            <strong>{{ row.name }}</strong>
            <div class="muted">
              {{ row.bot_name }} · {{ t('selfReminders.schedule') }}
            </div>
          </div>
          <div class="tags">
            <el-tag
              effect="plain"
              type="info"
            >
              {{ t(row.schedule_kind === 'once' ? 'selfReminders.once' : 'selfReminders.recurring') }}
            </el-tag>
            <el-tag :type="statusTag(row).type">
              {{ statusTag(row).text }}
            </el-tag>
          </div>
        </header>
        <dl>
          <dt>{{ t('selfReminders.when') }}</dt>
          <dd v-if="row.schedule_kind === 'once'">
            {{ time(row.run_at, row.timezone) }}
          </dd>
          <dd v-else>
            <code>{{ row.cron_expression }}</code> · {{ row.timezone }}
          </dd>
          <dt>{{ t('selfReminders.next') }}</dt>
          <dd>{{ row.enabled ? time(row.next_run_at, row.timezone) : '—' }}</dd>
          <dt>{{ t('selfReminders.statusLabel') }}</dt>
          <dd>{{ label('selfReminders.scheduleStatus', row.running ? 'running' : row.status) }}</dd>
        </dl>
        <details class="instruction">
          <summary>{{ t('selfReminders.instruction') }}</summary>
          <p class="text">
            {{ row.text }}
          </p>
        </details>
        <div class="last-run">
          <div class="muted">
            {{ t('selfReminders.lastRun') }}
          </div>
          <p v-if="!row.last_run">
            {{ t('selfReminders.noRun') }}
          </p>
          <template v-else>
            <p>
              {{ label('cron.status', row.last_run.status) }} · {{ time(row.last_run.started_at, row.timezone) }}<span v-if="row.last_run.deliveries.length"> · {{ t('selfReminders.delivery') }}: {{ row.last_run.deliveries.map(state => label('selfReminders.status', state)).join(' / ') }}</span>
            </p>
            <p
              v-if="row.last_run.error"
              class="run-error"
            >
              {{ t('selfReminders.error') }}: {{ row.last_run.error }}
            </p>
            <pre
              v-if="row.last_run.reply"
              class="reply"
            >{{ row.last_run.reply }}</pre>
            <p
              v-if="row.last_run.truncated"
              class="muted"
            >
              {{ t('selfReminders.truncated') }}
            </p>
          </template>
        </div>
        <p
          v-if="!row.enabled && !row.can_resume"
          class="muted"
        >
          {{ t('selfReminders.expiredOnce') }}
        </p>
        <footer class="actions">
          <el-button
            v-if="row.can_pause"
            :disabled="busy"
            :data-test="'pause-' + row.id"
            @click="pause(row)"
          >
            {{ t('selfReminders.pause') }}
          </el-button>
          <el-button
            v-if="row.can_resume"
            type="primary"
            :disabled="busy"
            :data-test="'resume-' + row.id"
            @click="resume(row)"
          >
            {{ t('selfReminders.resume') }}
          </el-button>
          <el-button
            type="danger"
            plain
            :disabled="busy"
            :data-test="'delete-' + row.id"
            @click="remove(row)"
          >
            {{ t('selfReminders.delete') }}
          </el-button>
        </footer>
      </article>
      <article
        v-else
        class="reminder"
      >
        <strong>{{ row.bot_name }} · {{ t('selfReminders.fixed') }}</strong>
        <p>{{ time(row.run_at) }}</p>
        <p class="text">
          {{ row.text }}
        </p>
        <p>
          {{ t('selfReminders.status.' + row.status) }}<span
            v-for="(state, index) in row.deliveries"
            :key="index"
          > · {{ t('selfReminders.status.' + state) }}</span>
        </p>
        <el-button
          v-if="row.can_cancel"
          :disabled="busy"
          :data-test="'cancel-' + row.id"
          @click="cancel(row.id)"
        >
          {{ t('selfReminders.cancel') }}
        </el-button>
      </article>
    </template>
  </section>
</template>
<style scoped>
.reminders { max-width: 880px; margin: 0 auto; }
.reminders > p { color: var(--el-text-color-secondary); line-height: 1.7; }
.reminder { margin-top: 16px; padding: 20px; border: 1px solid var(--el-border-color); border-radius: 12px; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.7; }
.schedule-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.schedule-heading strong { overflow-wrap: anywhere; }
.tags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
dl { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 8px 14px; margin: 16px 0; font-size: 14px; line-height: 1.7; }
dt { color: var(--el-text-color-secondary); }
dd { margin: 0; overflow-wrap: anywhere; }
.instruction { margin: 12px 0; }
.instruction summary { cursor: pointer; color: var(--el-text-color-secondary); font-size: 13px; }
.instruction summary:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 4px; }
.instruction .text { margin: 8px 0 0; padding: 12px; border-radius: 8px; background: var(--el-fill-color-light); max-height: 240px; overflow-y: auto; }
.last-run { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--el-border-color-lighter); }
.last-run p { margin: 6px 0; line-height: 1.7; }
.run-error { color: var(--el-color-danger); overflow-wrap: anywhere; }
.reply { margin: 8px 0; padding: 12px; border-radius: 8px; background: var(--el-fill-color-light); white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; font-size: 13px; line-height: 1.7; max-height: 360px; overflow-y: auto; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.actions .el-button + .el-button { margin-left: 0; }
@media (max-width: 600px) {
  .schedule-heading { flex-direction: column; }
  .tags { justify-content: flex-start; }
  dl { grid-template-columns: 1fr; gap: 2px; } dd { margin-bottom: 8px; }
}
</style>
