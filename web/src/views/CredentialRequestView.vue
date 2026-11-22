<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { personalCredentials, type CredentialRequest } from '@/api/personalCredentials'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const route = useRoute()
const id = computed(() => String(route.params.id))
const form = ref<CredentialRequest | null>(null)
const values = reactive<Record<string, string>>({})
/** 选用的已有凭证：{变量名: 凭证 ID}。同一项又填了值时以填的为准。 */
const picks = reactive<Record<string, string>>({})
const error = ref('')
const busy = ref(false)
const saved = ref(false)
/** 服务端按实际结果写的一句话：续接、下次对话或下次执行时生效，不能一律承诺「继续任务」。 */
const outcome = ref('')
/** 一次性交付不保存：标题和结果都不能说「个人凭证」「已保存」。 */
const once = computed(() => form.value?.save === false)
const choices = (key: string) => form.value?.choices?.[key] ?? []

async function load() {
  busy.value = true
  try {
    form.value = await personalCredentials.request(id.value)
    Object.assign(picks, form.value.defaults ?? {})
    error.value = ''
  }
  catch (e) { error.value = errorMessage(e) || t('credentialRequest.loadError') }
  finally { busy.value = false }
}
/** 提交成功后立即清空输入框：值只在这一次请求里离开浏览器。 */
async function submit() {
  if (!form.value || busy.value) return
  const typed = (key: string) => !!(values[key] ?? '').trim()
  const missing = form.value.fields.find(f => !typed(f.key) && !picks[f.key])
  if (missing) { error.value = t('credentialRequest.required', { label: missing.label }); return }
  busy.value = true
  try {
    const result = await personalCredentials.submit(
      id.value,
      Object.fromEntries(form.value.fields.filter(f => typed(f.key)).map(f => [f.key, values[f.key]])),
      Object.fromEntries(form.value.fields.filter(f => !typed(f.key) && picks[f.key]).map(f => [f.key, picks[f.key]])),
    )
    outcome.value = result.message
    for (const key of Object.keys(values)) values[key] = ''
    saved.value = true
    error.value = ''
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <section class="credential-request">
    <h1>{{ once ? t('credentialRequest.titleOnce') : t('credentialRequest.title') }}</h1>
    <el-alert
      v-if="error"
      data-test="error"
      :title="error"
      type="error"
      :closable="false"
    />
    <el-result
      v-if="saved"
      data-test="saved"
      icon="success"
      :title="once ? t('credentialRequest.handedOff') : t('credentialRequest.saved')"
      :sub-title="outcome || t('credentialRequest.savedHint')"
    />
    <template v-else-if="form">
      <p class="muted">
        {{ t('credentialRequest.from', { bot: form.bot_name }) }}
      </p>
      <p><strong>{{ t('credentialRequest.purpose') }}</strong> {{ form.purpose }}</p>
      <el-alert
        v-if="form.status !== 'open'"
        data-test="closed"
        :title="t(`credentialRequest.closed.${form.status}`)"
        type="warning"
        :closable="false"
      />
      <el-form
        v-else
        label-position="top"
        @submit.prevent="submit"
      >
        <el-form-item
          v-for="field in form.fields"
          :key="field.key"
          :label="field.label"
        >
          <el-select
            v-if="choices(field.key).length"
            v-model="picks[field.key]"
            class="pick"
            :data-test="'pick-' + field.key"
            clearable
            filterable
            :placeholder="t('credentialRequest.pick')"
          >
            <el-option
              v-for="choice in choices(field.key)"
              :key="choice.id"
              :label="`${choice.label || choice.env_key}（${choice.env_key}）`"
              :value="choice.id"
            >
              <span>{{ choice.label || choice.env_key }}（{{ choice.env_key }}）</span>
              <span
                v-if="choice.bots.length"
                class="muted used-by"
              >{{ t('credentialRequest.usedBy', { bots: choice.bots.join('、') }) }}</span>
            </el-option>
          </el-select>
          <el-input
            v-model="values[field.key]"
            :data-test="'field-' + field.key"
            :type="field.secret ? 'password' : 'text'"
            :show-password="field.secret"
            :placeholder="picks[field.key] ? t('credentialRequest.orTypeNew') : field.placeholder"
            :maxlength="4096"
            :autocomplete="field.secret ? 'new-password' : 'off'"
          />
        </el-form-item>
        <p class="muted">
          {{ t('credentialRequest.expiresAt', { time: formatDateTime(form.expires_at) }) }}
        </p>
        <el-button
          type="primary"
          native-type="submit"
          :loading="busy"
        >
          {{ t('credentialRequest.submit') }}
        </el-button>
      </el-form>
      <p class="note">
        {{ form.security_note }}
      </p>
    </template>
  </section>
</template>
<style scoped>
.credential-request { max-width: 560px; }
.muted, .note { color: var(--el-text-color-secondary); }
.note { font-size: 12px; margin-top: 16px; }
.pick { width: 100%; margin-bottom: 8px; }
.used-by { float: right; margin-left: 12px; font-size: 12px; }
</style>
