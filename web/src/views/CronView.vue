<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { useUnsavedChanges } from '@/composables/useUnsavedChanges'
import LoadState from '@/components/LoadState.vue'
import { useListQuery } from '@/composables/useListQuery'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots, users } from '@/api/admin'
import { cron, type CronIn, type CronOut, type CronRun } from '@/api/cron'
import type { BotOut, UserOut } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const rows = ref<CronOut[]>([]), total = ref(0), page = ref(1), filter = ref('')
const botOptions = ref<BotOut[]>([]), selectedUsers = ref<UserOut[]>([])
const loading = ref(false), saving = ref(false), visible = ref(false), editing = ref<CronOut | null>(null)
const expires = ref<Date | null>(null), emails = ref(''), precheckResult = ref('')
const historyVisible = ref(false), historyLoading = ref(false), history = ref<CronRun[]>([])
const historyJob = ref<CronOut | null>(null), historyPage = ref(1), historyTotal = ref(0)
const template = 'def should_trigger(ctx):\n    return {"trigger": True, "reason": "ready"}'
function empty(): CronIn {
  return { bot_id: '', name: '', cron_expression: '0 9 * * 1-5', timezone: 'Asia/Shanghai', prompt: '',
    system_prompt: null, precheck_script: null, precheck_timeout_seconds: 30, enabled: true,
    expires_at: null, target_users: [], target_chats: [], notify_emails: [], notify_webhook: false, notify_webhook_url: null }
}
const form = reactive<CronIn>(empty())
const chatOptions = ref<string[]>([])
const notificationTesting = ref(false), notificationTestVisible = ref(false)
const testJob = ref<CronOut | null>(null), testDeliveries = ref<CronRun['deliveries']>([])
const testErrors = ref<Record<string, string>>({})
const selectedPlatform = computed(() => botOptions.value.find(b => b.id === form.bot_id)?.platform)
watch(() => form.bot_id, async (id, previous) => {
  if (previous && !editing.value) { form.target_users = []; form.target_chats = []; selectedUsers.value = [] }
  chatOptions.value = []
  if (!id) return
  try { const options = await cron.notificationChats(id); if (id === form.bot_id) chatOptions.value = options }
  catch (e) { fail(e) }
})
async function refreshNotificationTests() {
  if (!testJob.value) return
  try { testDeliveries.value = await cron.notificationTests(testJob.value.id) } catch (e) { fail(e) }
}
async function sendTest(row: CronOut) {
  if (notificationTesting.value) return
  notificationTesting.value = true
  try {
    const result = await cron.testNotification(row)
    testJob.value = row; testErrors.value = result.errors; testDeliveries.value = []
    notificationTestVisible.value = true
    await refreshNotificationTests()
  } catch (e) { fail(e) } finally { notificationTesting.value = false }
}
function deliveryStatus(status: string) {
  if (!status) return '—'
  const key = `notification.status.${status}`
  return t(key) === key ? status : t(key)
}
let originalForm = ''
const { confirmDiscard } = useUnsavedChanges(() => visible.value && !!originalForm && originalForm !== JSON.stringify(payload()))
async function closeEditor(done: () => void) { if (await confirmDiscard()) done() }
function fail(e: unknown) { ElMessage.error(errorMessage(e)) }
const listLoading = ref(false), listError = ref('')
const { persist: persistQuery } = useListQuery({ page, bot_id: filter }, () => { void load() })
async function load() {
  persistQuery(); listLoading.value = true; listError.value = ''

  loading.value = true
  try { const data = await cron.list(page.value, filter.value || undefined); rows.value = data.items; total.value = data.total }
  catch (e) { listError.value = errorMessage(e); fail(e) } finally { listLoading.value = false; loading.value = false }
}
async function searchBots(keyword = '') {
  try { botOptions.value = (await bots.list({ scope: 'mine', keyword, per_page: 100 })).items }
  catch (e) { fail(e) }
}
onMounted(() => { void load(); void searchBots() })
function payload(): CronIn {
  const split = (s: string) => [...new Set(s.split(/[,\n]/).map(x => x.trim()).filter(Boolean))]
  return { ...form, target_users: [...form.target_users], target_chats: [...form.target_chats], notify_emails: split(emails.value),
    expires_at: expires.value?.toISOString() ?? null }
}
async function open(row?: CronOut) {
  editing.value = row ?? null
  Object.assign(form, empty())
  if (row) for (const key of Object.keys(empty()) as (keyof CronIn)[]) Object.assign(form, { [key]: row[key] })
  form.target_users = [...form.target_users]
  expires.value = row?.expires_at ? new Date(row.expires_at) : null
  form.target_chats = [...form.target_chats]; emails.value = form.notify_emails.join('\n')
  form.notify_webhook_url = null;
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
  saving.value = true
  try {
    if (editing.value) await cron.update(editing.value.id, payload(), editing.value.version)
    else await cron.create(payload())
    visible.value = false; ElMessage.success(t('common.saved')); await load()
  } catch (e) { fail(e) } finally { saving.value = false }
}
async function run(row: CronOut) {
  saving.value = true
  try { await cron.run(row); ElMessage.success(t('cron.queued')); await load() }
  catch (e) { fail(e) } finally { saving.value = false }
}
async function cancelRun(row: CronOut) {
  try { await cron.cancel(row); await load() } catch (e) { fail(e) }
}
async function disable(row: CronOut) {
  try { await cron.disable(row); await load() } catch (e) { fail(e) }
}
async function remove(row: CronOut) {
  try { await ElMessageBox.confirm(t('common.confirmDelete')); await cron.remove(row); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) }
}
async function preview() {
  try {
    const result = await cron.precheck(form.precheck_script || template)
    precheckResult.value = result.error || `${t(result.trigger ? 'cron.willRun' : 'cron.willSkip')} · ${result.reason || ''}`
  } catch (e) { fail(e) }
}
async function loadHistory() {
  if (!historyJob.value) return
  historyLoading.value = true
  try { const data = await cron.runs(historyJob.value.id, historyPage.value); history.value = data.items; historyTotal.value = data.total }
  catch (e) { fail(e) } finally { historyLoading.value = false }
}
function showHistory(row: CronOut) {
  historyJob.value = row; history.value = []; historyPage.value = 1; historyVisible.value = true; void loadHistory()
}
</script>

<template>
  <section class="cron-page">
    <div class="toolbar cm-page-header">
      <h2>{{ t('menu.cron') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.cron') }}
      </p><el-button
        type="primary"
        data-test="create-cron"
        @click="open()"
      >
        {{ t('common.create') }}
      </el-button>
    </div>
    <p class="hint">
      {{ t('cron.identityHint') }}
    </p>
    <div class="toolbar">
      <el-select
        v-model="filter"
        clearable
        filterable
        remote
        :remote-method="searchBots"
        :placeholder="t('cron.bot')"
        @change="page = 1; load()"
      >
        <el-option
          v-for="bot in botOptions"
          :key="bot.id"
          :label="bot.name"
          :value="bot.id"
        />
      </el-select>
      <el-button @click="load">
        {{ t('common.refresh') }}
      </el-button>
    </div>
    <LoadState
      :loading="listLoading"
      :error="listError"
      @retry="load"
    />
    <el-table
      v-loading="loading"
      :data="rows"
      row-key="id"
      empty-text="—"
    >
      <el-table-column
        prop="name"
        :label="t('cron.name')"
        min-width="150"
      />
      <el-table-column
        :label="t('cron.schedule')"
        min-width="180"
      >
        <template #default="{ row }">
          <code>{{ row.cron_expression }}</code><div class="hint">
            {{ row.timezone }}
          </div>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('cron.next')"
        min-width="170"
      >
        <template #default="{ row }">
          {{ row.enabled ? formatDateTime(row.next_run_at) : t('common.disabled') }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('cron.lastStatus')"
        min-width="110"
      >
        <template #default="{ row }">
          {{ row.force_run_at ? t('cron.queued') : row.last_status ? t(`cron.status.${row.last_status}`) : '—' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('common.actions')"
        min-width="350"
      >
        <template #default="{ row }">
          <el-button
            link
            :data-test="`history-${row.id}`"
            @click="showHistory(row)"
          >
            {{ t('cron.history') }}
          </el-button>
          <el-button
            link
            :disabled="!row.enabled || !!row.running_task_id || !!row.force_run_at || saving"
            :data-test="`run-${row.id}`"
            @click="run(row)"
          >
            {{ t('cron.run') }}
          </el-button>
          <el-button
            v-if="row.can_edit"
            link
            :disabled="notificationTesting"
            :data-test="`test-notification-${row.id}`"
            @click="sendTest(row)"
          >
            {{ t('notification.test') }}
          </el-button>
          <el-button
            v-if="row.can_edit"
            link
            :disabled="!!row.force_run_at"
            :data-test="`edit-${row.id}`"
            @click="open(row)"
          >
            {{ t('common.edit') }}
          </el-button>
          <el-button
            v-if="row.running_task_id || row.force_run_at"
            link
            type="warning"
            @click="cancelRun(row)"
          >
            {{ t('cron.cancelRun') }}
          </el-button>
          <el-button
            v-if="row.enabled"
            link
            @click="disable(row)"
          >
            {{ t('common.disable') }}
          </el-button>
          <el-button
            v-if="row.can_edit"
            link
            type="danger"
            :disabled="!!row.running_task_id"
            @click="remove(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="page"
      :page-size="50"
      :total="total"
      layout="prev, pager, next, total"
      @current-change="load"
    />
    <el-dialog
      v-model="visible"
      :close-on-click-modal="false"
      :before-close="closeEditor"
      :title="t(editing ? 'common.edit' : 'common.create')"
      width="min(780px, 95vw)"
      destroy-on-close
    >
      <el-form
        label-position="top"
        @submit.prevent="save"
      >
        <h3 class="cm-section-title">
          {{ t('workspace.basic') }}
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
          <el-form-item :label="t('cron.schedule')">
            <el-input
              v-model="form.cron_expression"
              placeholder="0 9 * * 1-5"
            />
          </el-form-item>
          <el-form-item :label="t('cron.timezone')">
            <el-input v-model="form.timezone" />
          </el-form-item>
        </div>
        <p class="hint">
          {{ t('cron.scheduleHint') }}
        </p>
        <h3 class="cm-section-title">
          {{ t('workspace.configuration') }}
        </h3>
        <el-form-item
          :label="t('cron.prompt')"
          required
        >
          <el-input
            v-model="form.prompt"
            type="textarea"
            :rows="4"
            maxlength="32000"
          />
        </el-form-item>
        <el-form-item :label="t('cron.systemPrompt')">
          <el-input
            v-model="form.system_prompt"
            type="textarea"
            :rows="2"
            maxlength="32000"
          />
        </el-form-item>
        <div class="grid">
          <el-form-item :label="t('cron.expires')">
            <el-date-picker
              v-model="expires"
              type="datetime"
              clearable
            />
          </el-form-item><el-form-item :label="t('common.enable')">
            <el-switch v-model="form.enabled" />
          </el-form-item>
        </div>
        <el-divider>{{ t('notification.title') }}</el-divider>
        <p class="hint">
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
                :key="chat"
                :label="chat"
                :value="chat"
              />
            </el-select>
          </el-form-item><el-form-item :label="t('cron.emails')">
            <el-input
              v-model="emails"
              type="textarea"
              :placeholder="t('cron.perLine')"
            />
          </el-form-item>
        </div>
        <el-checkbox v-model="form.notify_webhook">
          {{ t('notification.webhook') }}
        </el-checkbox>
        <p class="hint">
          {{ t('notification.webhookHint') }}
        </p>
        <el-form-item
          v-if="form.notify_webhook"
          :label="t('notification.address')"
        >
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
          <p class="hint">
            {{ t('notification.saveToTest') }}
          </p>
        </el-form-item>
        <el-collapse class="precheck">
          <el-collapse-item :title="t('cron.precheck')">
            <p class="hint">
              {{ t('cron.precheckHint') }}
            </p>
            <el-input
              v-model="form.precheck_script"
              type="textarea"
              :rows="8"
              :placeholder="template"
              maxlength="32768"
            />
            <el-button @click="form.precheck_script = template">
              {{ t('cron.template') }}
            </el-button><el-button @click="preview">
              {{ t('cron.test') }}
            </el-button>
            <p
              v-if="precheckResult"
              role="status"
            >
              {{ precheckResult }}
            </p>
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
    <el-dialog
      v-model="notificationTestVisible"
      :title="t('notification.testResults')"
      width="min(760px, 95vw)"
    >
      <p class="hint">
        {{ t('notification.testHint') }}
      </p>
      <p
        v-for="(error, target) in testErrors"
        :key="target"
      >
        {{ target }}: {{ error }}
      </p>
      <el-button @click="refreshNotificationTests">
        {{ t('common.refresh') }}
      </el-button>
      <el-table :data="testDeliveries">
        <el-table-column :label="t('cron.channel')">
          <template #default="{ row }">
            {{ row.channel === 'webhook' ? t('notification.webhook') : row.platform }}
          </template>
        </el-table-column>
        <el-table-column :label="t('cron.deliveryStatus')">
          <template #default="{ row }">
            {{ deliveryStatus(row.status) }}
          </template>
        </el-table-column>
        <el-table-column
          prop="error"
          :label="t('cron.error')"
        />
      </el-table>
    </el-dialog>
    <el-drawer
      v-model="historyVisible"
      :title="`${t('cron.history')} · ${historyJob?.name || ''}`"
      size="min(850px, 95vw)"
    >
      <el-button @click="loadHistory">
        {{ t('common.refresh') }}
      </el-button>
      <el-table
        v-loading="historyLoading"
        :data="history"
        row-key="id"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="run-detail">
              <b>{{ t('cron.prompt') }}</b><pre>{{ row.prompt }}</pre><b>{{ t('cron.reply') }}</b><pre>{{ row.reply || row.error_message || '—' }}</pre>
              <p>{{ t('cron.precheck') }}: {{ row.precheck_meta || '—' }}</p>
              <p v-if="row.delivery.errors && Object.keys(row.delivery.errors).length">
                {{ row.delivery.errors }}
              </p>
              <el-table :data="row.deliveries">
                <el-table-column
                  min-width="140"
                  prop="platform"
                  :label="t('cron.channel')"
                >
                  <template #default="{ row: delivery }">
                    {{ delivery.channel === 'webhook' ? t('notification.webhook') : delivery.platform === 'wecom' || delivery.platform === 'feishu' ? t(`platforms.${delivery.platform}`) : delivery.platform }}
                  </template>
                </el-table-column><el-table-column
                  min-width="140"
                  prop="status"
                  :label="t('cron.deliveryStatus')"
                >
                  <template #default="{ row: delivery }">
                    {{ deliveryStatus(delivery.status) }}
                  </template>
                </el-table-column><el-table-column
                  min-width="140"
                  prop="error"
                  :label="t('cron.error')"
                />
              </el-table>
            </div>
          </template>
        </el-table-column>
        <el-table-column
          :label="t('cron.started')"
          min-width="170"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.started_at) }}
          </template>
        </el-table-column>
        <el-table-column
          min-width="140"
          :label="t('cron.trigger')"
        >
          <template #default="{ row }">
            {{ t(`cron.${row.trigger_kind}`) }}
          </template>
        </el-table-column>
        <el-table-column
          min-width="140"
          :label="t('cron.lastStatus')"
        >
          <template #default="{ row }">
            {{ t(`cron.status.${row.status}`) }}
          </template>
        </el-table-column>
        <el-table-column
          min-width="140"
          :label="t('cron.tokens')"
        >
          <template #default="{ row }">
            {{ row.input_tokens ?? '—' }} / {{ row.output_tokens ?? '—' }}
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="historyPage"
        :page-size="50"
        :total="historyTotal"
        layout="prev, pager, next, total"
        @current-change="loadHistory"
      />
    </el-drawer>
  </section>
</template>
<style scoped>
.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.hint { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
.el-select, .el-date-editor { width: 100%; }
.toolbar .el-select { max-width: 320px; }
.precheck { margin-top: 20px; }
.run-detail { padding: 12px 24px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; }
@media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
</style>
