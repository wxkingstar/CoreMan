<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { useUnsavedChanges } from '@/composables/useUnsavedChanges'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { users } from '@/api/admin'
import { cron, type CronIn, type CronOut } from '@/api/cron'
import type { BotOut, UserOut } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'

const props = defineProps<{ botOptions: BotOut[] }>()
const emit = defineEmits<{ saved: []; searchBots: [keyword: string] }>()
const { t } = useI18n()
const saving = ref(false), visible = ref(false), editing = ref<CronOut | null>(null)
const selectedUsers = ref<UserOut[]>([])
const expires = ref<Date | null>(null), emails = ref(''), precheckResult = ref('')
const template = 'def should_trigger(ctx):\n    return {"trigger": True, "reason": "ready"}'
function empty(): CronIn {
  return { bot_id: '', name: '', cron_expression: '0 9 * * 1-5', timezone: 'Asia/Shanghai', prompt: '',
    system_prompt: null, precheck_script: null, precheck_timeout_seconds: 30, enabled: true,
    expires_at: null, target_users: [], target_chats: [], notify_emails: [], notify_webhook: false, notify_webhook_url: null }
}
const form = reactive<CronIn>(empty())
const chatOptions = ref<string[]>([])
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
    visible.value = false; ElMessage.success(t('common.saved')); emit('saved')
  } catch (e) { fail(e) } finally { saving.value = false }
}
async function preview() {
  try {
    const result = await cron.precheck(form.precheck_script || template)
    precheckResult.value = result.error || `${t(result.trigger ? 'cron.willRun' : 'cron.willSkip')} · ${result.reason || ''}`
  } catch (e) { fail(e) }
}
defineExpose({ open })
</script>

<template>
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
</template>

<style scoped>
.hint { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
.el-select, .el-date-editor { width: 100%; }
.precheck { margin-top: 20px; }
@media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
</style>
