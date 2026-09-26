<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { useUnsavedChanges } from '@/composables/useUnsavedChanges'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { users } from '@/api/admin'
import { api } from '@/api/client'
import { cron, type CronIn, type CronOut } from '@/api/cron'
import type { BotOut, UserOut } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'
import { defaultSpec, pad, parseCron, presets, toCron, type ScheduleSpec } from '@/components/cron/schedule'

const props = defineProps<{ botOptions: BotOut[] }>()
const emit = defineEmits<{ saved: []; searchBots: [keyword: string] }>()
const { t } = useI18n()
const saving = ref(false), visible = ref(false), editing = ref<CronOut | null>(null)
const selectedUsers = ref<UserOut[]>([])
const runAt = ref<Date | null>(null)
const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone
const expires = ref<Date | null>(null), emails = ref(''), precheckResult = ref('')
const precheckOpen = ref<string[]>([])
const template = 'def should_trigger(ctx):\n    return {"trigger": True, "reason": "ready"}'
function empty(): CronIn {
  return { schedule_kind: 'recurring', run_at: null, bot_id: '', name: '', cron_expression: '0 9 * * 1-5', timezone: 'Asia/Shanghai', prompt: '',
    system_prompt: null, precheck_script: null, precheck_timeout_seconds: 30, enabled: true,
    expires_at: null, target_users: [], target_chats: [], notify_emails: [], notify_webhook: false, notify_webhook_url: null }
}
const form = reactive<CronIn>(empty())
const chatOptions = ref<{ id: string; name: string }[]>([])
const selectedPlatform = computed(() => props.botOptions.find(b => b.id === form.bot_id)?.platform)
watch(() => form.bot_id, async (id, previous) => {
  if (previous && !editing.value) { form.target_users = []; form.target_chats = []; selectedUsers.value = [] }
  chatOptions.value = []
  if (!id) return
  try { const options = await cron.notificationChats(id); if (id === form.bot_id) chatOptions.value = options }
  catch (e) { fail(e) }
})
let originalForm = ''
const { confirmDiscard } = useUnsavedChanges(() => visible.value && !!originalForm && originalForm !== JSON.stringify(payload()))
async function closeEditor(done: () => void) { if (await confirmDiscard()) done() }
function fail(e: unknown) { ElMessage.error(errorMessage(e)) }
/** 机器人下拉的远程搜索交给页面：页面顶部的筛选框用的是同一份选项。 */
function searchBots(keyword = '') { emit('searchBots', keyword) }
function payload(): CronIn {
  const split = (s: string) => [...new Set(s.split(/[,\n]/).map(x => x.trim()).filter(Boolean))]
  return { ...form, target_users: [...form.target_users], target_chats: [...form.target_chats], notify_emails: split(emails.value),
    expires_at: expires.value?.toISOString() ?? null,
    run_at: form.schedule_kind === 'once' ? runAt.value?.toISOString() ?? null : null,
    timezone: onceTimeChanged() ? localZone : form.timezone }
}
function onceTimeChanged(): boolean {
  return form.schedule_kind === 'once' && (!editing.value || editing.value.schedule_kind !== 'once' || runAt.value?.getTime() !== new Date(editing.value.run_at || 0).getTime())
}
async function open(row?: CronOut) {
  editing.value = row ?? null
  Object.assign(form, empty())
  if (row) for (const key of Object.keys(empty()) as (keyof CronIn)[]) Object.assign(form, { [key]: row[key] })
  form.schedule_kind = row?.schedule_kind ?? 'recurring'
  runAt.value = row?.run_at ? new Date(row.run_at) : null
  form.target_users = [...form.target_users]
  expires.value = row?.expires_at ? new Date(row.expires_at) : null
  form.target_chats = [...form.target_chats]; emails.value = form.notify_emails.join('\n')
  form.notify_webhook_url = null;
  Object.assign(schedule, parseCron(form.cron_expression) ?? { ...defaultSpec(), preset: 'custom' })
  precheckOpen.value = form.precheck_script ? ['precheck'] : []
  precheckResult.value = ''; selectedUsers.value = []; originalForm = JSON.stringify(payload()); visible.value = true
  if (row?.target_users.length) {
    const values = await Promise.allSettled(row.target_users.map(id => users.get(id)))
    if (editing.value?.id === row.id) selectedUsers.value = values.flatMap(v => v.status === 'fulfilled' ? [v.value] : [])
  }
}
async function save() {
  if (saving.value) return
  if (!form.name.trim() || !form.bot_id || !form.prompt.trim()) { ElMessage.warning(t('cron.required')); return }
  if (expires.value && expires.value.getTime() <= Date.now() && form.enabled) { ElMessage.warning(t('cron.expired')); return }
  const onceChanged = onceTimeChanged()
  if (form.schedule_kind === 'once' && (!runAt.value || (onceChanged && runAt.value.getTime() <= Date.now()))) { ElMessage.warning(t('cronOnce.future')); return }
  saving.value = true
  try {
    if (form.schedule_kind === 'once') {
      const recipients = await Promise.all(form.target_users.map(async id => {
        const known = selectedUsers.value.find(user => user.id === id)
        if (known) return known.display_name
        try { return (await users.get(id)).display_name } catch { return id }
      }))
      recipients.push(...form.target_chats.map(id => chatOptions.value.find(chat => chat.id === id)?.name || id), ...payload().notify_emails)
      if (form.notify_webhook) recipients.push(t('notification.webhook'))
      if (!recipients.length) {
        const creator = editing.value?.created_by ? await users.get(editing.value.created_by) : await api.me()
        recipients.push(`${creator.display_name} · ${t('cronOnce.creator')}`)
      }
      await ElMessageBox.confirm(`${t('cronOnce.once')} · ${runAt.value!.toString()} (${localZone})\n${t('cron.recipients')}: ${recipients.join('、') || t('cronOnce.creator')}`, t('cronOnce.confirm'))
    }
    if (editing.value) await cron.update(editing.value.id, payload(), editing.value.version)
    else await cron.create(payload())
    visible.value = false; ElMessage.success(t('common.saved')); emit('saved')
  } catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) } finally { saving.value = false }
}
async function preview() {
  try {
    const result = await cron.precheck(form.precheck_script || template)
    precheckResult.value = result.error || `${t(result.trigger ? 'cron.willRun' : 'cron.willSkip')} · ${result.reason || ''}`
  } catch (e) { fail(e) }
}
/** 快捷选择只在用户改动时回写表达式，打开已有任务不会改写原表达式。 */
const schedule = reactive<ScheduleSpec>(defaultSpec())
function setSchedule(patch: Partial<ScheduleSpec>) {
  Object.assign(schedule, patch)
  if (schedule.preset !== 'custom') form.cron_expression = toCron(schedule)
}
const scheduleTime = computed(() => `${pad(schedule.hour)}:${pad(schedule.minute)}`)
function setTime(value: string | null) {
  const [hour, minute] = (value || '09:00').split(':').map(Number)
  setSchedule({ hour, minute })
}
const weekdayOrder = [1, 2, 3, 4, 5, 6, 0]
const withCurrent = (values: number[], current: number) => [...new Set([...values, current])].sort((a, b) => a - b)
const hourOptions = computed(() => withCurrent([1, 2, 3, 4, 6, 8, 12], schedule.hours))
const minuteOptions = computed(() => withCurrent([5, 10, 15, 20, 30], schedule.interval))
const scheduleSummary = computed(() => {
  const spec = parseCron(form.cron_expression)
  if (!spec) return t('cronSchedule.summary.custom')
  const time = `${pad(spec.hour)}:${pad(spec.minute)}`, separator = t('cronSchedule.separator')
  switch (spec.preset) {
    case 'weekly': return t('cronSchedule.summary.weekly', { time, days: weekdayOrder.filter(d => spec.weekdays.includes(d)).map(d => t(`cronSchedule.days.${d}`)).join(separator) })
    case 'monthly': return t('cronSchedule.summary.monthly', { time, days: spec.monthDays.join(separator) })
    case 'hourly': return spec.hours > 1 ? t('cronSchedule.summary.hoursN', { n: spec.hours, minute: pad(spec.minute) }) : t('cronSchedule.summary.hourly', { minute: pad(spec.minute) })
    case 'minutes': return spec.interval > 1 ? t('cronSchedule.summary.minutes', { n: spec.interval }) : t('cronSchedule.summary.everyMinute')
    default: return t(`cronSchedule.summary.${spec.preset}`, { time })
  }
})
const frequent = computed(() => {
  const spec = parseCron(form.cron_expression)
  return spec?.preset === 'minutes' && spec.interval < 30
})
const timezones = (() => {
  try { return (Intl as unknown as { supportedValuesOf(key: string): string[] }).supportedValuesOf('timeZone') } catch { return [] }
})()
const timezoneOptions = computed(() => [...new Set(['Asia/Shanghai', 'Asia/Tokyo', 'UTC', form.timezone, ...timezones])].filter(Boolean))
defineExpose({ open })
</script>

<template>
  <el-dialog
    v-model="visible"
    :close-on-click-modal="false"
    :before-close="closeEditor"
    :title="t(editing ? 'common.edit' : 'common.create')"
    width="min(760px, 95vw)"
    class="cron-editor-dialog"
    destroy-on-close
  >
    <el-form
      label-position="top"
      class="cron-form"
      @submit.prevent="save"
    >
      <h3 class="cm-section-title">
        <span>01</span>{{ t('workspace.basic') }}
      </h3>
      <div class="grid">
        <el-form-item
          :label="t('cron.name')"
          required
        >
          <el-input
            v-model="form.name"
            maxlength="100"
          />
        </el-form-item>
        <el-form-item
          :label="t('cron.bot')"
          required
        >
          <el-select
            v-model="form.bot_id"
            :disabled="!!editing"
            filterable
            remote
            :remote-method="searchBots"
          >
            <el-option
              v-for="bot in botOptions"
              :key="bot.id"
              :label="bot.name"
              :value="bot.id"
            />
          </el-select>
        </el-form-item>
      </div>

      <h3 class="cm-section-title">
        <span>02</span>{{ t('cronSchedule.section') }}
      </h3>
      <div class="grid">
        <el-form-item :label="t('cronOnce.kind')">
          <el-radio-group
            v-model="form.schedule_kind"
            class="kind"
            :disabled="!!editing?.running_task_id"
          >
            <el-radio-button value="recurring">
              {{ t('cronOnce.recurring') }}
            </el-radio-button>
            <el-radio-button value="once">
              {{ t('cronOnce.once') }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          v-if="form.schedule_kind === 'once'"
          :label="t('cronOnce.time')"
        >
          <el-date-picker
            v-model="runAt"
            type="datetime"
            format="YYYY-MM-DD HH:mm"
            :disabled="!!editing?.running_task_id"
          />
          <p class="hint">
            {{ t('cronOnce.local') }}: {{ localZone }}
          </p>
          <p
            v-if="editing?.consumed_at"
            class="hint"
          >
            {{ t('cronOnce.consumed') }}
          </p>
        </el-form-item>
        <el-form-item
          v-else
          :label="t('cron.timezone')"
        >
          <el-select
            v-model="form.timezone"
            filterable
            allow-create
            :reserve-keyword="false"
          >
            <el-option
              v-for="zone in timezoneOptions"
              :key="zone"
              :label="zone"
              :value="zone"
            />
          </el-select>
        </el-form-item>
      </div>
      <template v-if="form.schedule_kind !== 'once'">
        <el-form-item
          :label="t('cron.schedule')"
          class="stack"
        >
          <div class="schedule-row">
            <el-select
              :model-value="schedule.preset"
              class="preset"
              data-test="cron-preset"
              @update:model-value="setSchedule({ preset: $event })"
            >
              <el-option
                v-for="preset in presets"
                :key="preset"
                :label="t(`cronSchedule.presets.${preset}`)"
                :value="preset"
              />
            </el-select>
            <el-time-picker
              v-if="['daily', 'weekdays', 'weekly', 'monthly'].includes(schedule.preset)"
              :model-value="scheduleTime"
              class="time"
              format="HH:mm"
              value-format="HH:mm"
              :clearable="false"
              :aria-label="t('cronSchedule.time')"
              data-test="cron-time"
              @update:model-value="setTime"
            />
            <template v-else-if="schedule.preset === 'hourly'">
              <el-select
                :model-value="schedule.hours"
                class="unit"
                @update:model-value="setSchedule({ hours: $event })"
              >
                <el-option
                  v-for="n in hourOptions"
                  :key="n"
                  :label="t('cronSchedule.everyHours', { n })"
                  :value="n"
                />
              </el-select>
              <el-select
                :model-value="schedule.minute"
                class="unit"
                @update:model-value="setSchedule({ minute: $event })"
              >
                <el-option
                  v-for="n in 60"
                  :key="n - 1"
                  :label="t('cronSchedule.atMinute', { n: pad(n - 1) })"
                  :value="n - 1"
                />
              </el-select>
            </template>
            <el-select
              v-else-if="schedule.preset === 'minutes'"
              :model-value="schedule.interval"
              class="unit"
              @update:model-value="setSchedule({ interval: $event })"
            >
              <el-option
                v-for="n in minuteOptions"
                :key="n"
                :label="t('cronSchedule.everyMinutes', { n })"
                :value="n"
              />
            </el-select>
            <el-input
              v-else
              v-model="form.cron_expression"
              class="expression"
              placeholder="0 9 * * 1-5"
              data-test="cron-expression"
            />
          </div>
          <el-checkbox-group
            v-if="schedule.preset === 'weekly'"
            :model-value="schedule.weekdays"
            class="schedule-days"
            :aria-label="t('cronSchedule.weekdaysLabel')"
            @update:model-value="setSchedule({ weekdays: ($event as number[]).length ? $event as number[] : schedule.weekdays })"
          >
            <el-checkbox-button
              v-for="d in weekdayOrder"
              :key="d"
              :value="d"
            >
              {{ t(`cronSchedule.days.${d}`) }}
            </el-checkbox-button>
          </el-checkbox-group>
          <el-select
            v-if="schedule.preset === 'monthly'"
            :model-value="schedule.monthDays"
            class="schedule-days"
            multiple
            :placeholder="t('cronSchedule.monthDaysPlaceholder')"
            :aria-label="t('cronSchedule.monthDaysLabel')"
            @update:model-value="setSchedule({ monthDays: ($event as number[]).length ? $event as number[] : schedule.monthDays })"
          >
            <el-option
              v-for="n in 31"
              :key="n"
              :label="t('cronSchedule.dayOfMonth', { n })"
              :value="n"
            />
          </el-select>
          <div
            class="schedule-summary"
            role="status"
          >
            <span>{{ t('cronSchedule.summaryLabel') }}</span>
            <strong>{{ scheduleSummary }}</strong>
            <span class="zone">{{ form.timezone }}</span>
            <code>{{ form.cron_expression || '—' }}</code>
          </div>
          <p
            v-if="schedule.preset === 'custom'"
            class="hint"
          >
            {{ t('cronSchedule.customHint') }}
          </p>
          <p
            v-if="schedule.preset === 'monthly' && schedule.monthDays.some(d => d > 28)"
            class="hint"
          >
            {{ t('cronSchedule.monthEnd') }}
          </p>
          <p
            v-if="frequent"
            class="hint warn"
          >
            {{ t('cronSchedule.frequent') }}
          </p>
        </el-form-item>
      </template>

      <h3 class="cm-section-title">
        <span>03</span>{{ t('workspace.configuration') }}
      </h3>
      <el-form-item
        :label="t('cron.prompt')"
        required
      >
        <el-input
          v-model="form.prompt"
          type="textarea"
          :autosize="{ minRows: 4, maxRows: 12 }"
          maxlength="32000"
        />
      </el-form-item>
      <el-form-item :label="t('cron.systemPrompt')">
        <el-input
          v-model="form.system_prompt"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 8 }"
          maxlength="32000"
        />
      </el-form-item>
      <div class="grid">
        <el-form-item :label="t('cron.expires')">
          <el-date-picker
            v-model="expires"
            type="datetime"
            format="YYYY-MM-DD HH:mm"
            clearable
          />
        </el-form-item>
        <el-form-item :label="t('common.enable')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </div>

      <h3 class="cm-section-title">
        <span>04</span>{{ t('notification.title') }}
      </h3>
      <p class="hint section-hint">
        {{ t(selectedPlatform === 'feishu' ? 'notification.feishuHint' : selectedPlatform === 'wecom' ? 'notification.wecomHint' : 'notification.platformHint') }}
      </p>
      <el-form-item :label="t('cron.users')">
        <UserPicker
          v-model="form.target_users"
          :initial="selectedUsers"
        />
      </el-form-item>
      <div class="grid">
        <el-form-item :label="t('cron.groups')">
          <el-select
            v-model="form.target_chats"
            multiple
            filterable
            allow-create
            :reserve-keyword="false"
            :placeholder="t('notification.groupsHint')"
          >
            <el-option
              v-for="chat in chatOptions"
              :key="chat.id"
              :label="chat.name"
              :value="chat.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('cron.emails')">
          <el-input
            v-model="emails"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 4 }"
            :placeholder="t('cron.perLine')"
          />
        </el-form-item>
      </div>
      <el-form-item class="stack webhook">
        <el-checkbox v-model="form.notify_webhook">
          {{ t('notification.webhook') }}
        </el-checkbox>
        <p class="hint">
          {{ t('notification.webhookHint') }}
        </p>
      </el-form-item>
      <el-form-item
        v-if="form.notify_webhook"
        :label="t('notification.address')"
        class="stack"
      >
        <div class="webhook-row">
          <el-input
            :model-value="form.notify_webhook_url ?? ''"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="editing?.has_webhook_url ? t('notification.keepAddress') : 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…'"
            data-test="cron-webhook"
            @update:model-value="form.notify_webhook_url = ($event as string) || null"
          />
          <el-button
            v-if="editing?.has_webhook_url"
            link
            type="danger"
            @click="form.notify_webhook_url = ''; form.notify_webhook = false"
          >
            {{ t('notification.clearAddress') }}
          </el-button>
        </div>
        <p class="hint">
          {{ t('notification.saveToTest') }}
        </p>
      </el-form-item>

      <el-collapse
        v-model="precheckOpen"
        class="precheck"
      >
        <el-collapse-item
          name="precheck"
          :title="t('cron.precheck')"
        >
          <p class="hint precheck-hint">
            {{ t('cron.precheckHint') }}
          </p>
          <el-input
            v-model="form.precheck_script"
            type="textarea"
            class="code"
            :rows="8"
            :placeholder="template"
            maxlength="32768"
          />
          <div class="precheck-actions">
            <el-button @click="form.precheck_script = template">
              {{ t('cron.template') }}
            </el-button>
            <el-button @click="preview">
              {{ t('cron.test') }}
            </el-button>
            <span
              v-if="precheckResult"
              role="status"
            >
              {{ precheckResult }}
            </span>
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-form>
    <template #footer>
      <el-button @click="closeEditor(() => { visible = false })">
        {{ t('common.cancel') }}
      </el-button><el-button
        type="primary"
        :loading="saving"
        data-test="save-cron"
        @click="save"
      >
        {{ t('common.save') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint { margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.hint.warn { color: var(--el-color-warning); }
.section-hint { margin: -8px 0 16px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
.el-select, .grid :deep(.el-date-editor.el-input) { width: 100%; }
.cron-form > .cm-section-title { margin: 8px 0 16px; padding-top: 20px; }
.cron-form > .cm-section-title:first-child { margin-top: 0; padding-top: 0; border-top: 0; }
.stack :deep(.el-form-item__content) { display: block; }
.kind { display: flex; width: 100%; }
.kind :deep(.el-radio-button) { flex: 1; }
.kind :deep(.el-radio-button__inner) { width: 100%; }
.schedule-row { display: flex; flex-wrap: wrap; gap: 12px; }
.schedule-row .preset { width: 220px; }
.schedule-row .unit, .schedule-row :deep(.el-date-editor.time) { width: 150px; }
.schedule-row .expression { flex: 1; min-width: 200px; font-family: var(--el-font-family-mono, ui-monospace, monospace); }
.schedule-days { margin-top: 12px; }
.schedule-summary { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; margin-top: 12px; padding: 10px 14px; border-radius: 8px; background: var(--el-fill-color-light); font-size: 13px; line-height: 1.6; }
.schedule-summary > span { color: var(--el-text-color-secondary); }
.schedule-summary strong { font-weight: 600; }
.schedule-summary code { margin-left: auto; color: var(--el-text-color-secondary); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.webhook { margin-bottom: 12px; }
.webhook-row { display: flex; align-items: center; gap: 12px; }
.webhook-row .el-input { flex: 1; }
.precheck { margin-top: 8px; padding: 0; border-top: 1px solid var(--cm-border); background: transparent; border-radius: 0; }
.precheck :deep(.el-collapse-item__header), .precheck :deep(.el-collapse-item__wrap) { background: transparent; }
.precheck :deep(.el-collapse-item__header) { font-size: 15px; }
.precheck-hint { margin: 0 0 10px; }
.code :deep(textarea) { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
.precheck-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 0; margin-top: 12px; }
.precheck-actions > span { margin-left: 12px; color: var(--el-text-color-regular); font-size: 13px; }
@media (max-width: 600px) {
  .grid { grid-template-columns: 1fr; }
  .schedule-row .preset, .schedule-row .unit, .schedule-row :deep(.el-date-editor.time) { width: 100%; }
  .schedule-summary code { margin-left: 0; }
}
</style>

<style>
.cron-editor-dialog.el-dialog { max-height: 90dvh; display: flex; flex-direction: column; }
.cron-editor-dialog .el-dialog__body { min-height: 0; overflow-y: auto; }
.cron-editor-dialog .el-dialog__footer { flex-shrink: 0; }
</style>
