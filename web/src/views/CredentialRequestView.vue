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
const error = ref('')
const busy = ref(false)
const saved = ref(false)

async function load() {
  busy.value = true
  try { form.value = await personalCredentials.request(id.value); error.value = '' }
  catch (e) { error.value = errorMessage(e) || t('credentialRequest.loadError') }
  finally { busy.value = false }
}
/** 提交成功后立即清空输入框：值只在这一次请求里离开浏览器。 */
async function submit() {
  if (!form.value) return
  const missing = form.value.fields.find(f => !(values[f.key] ?? '').trim())
  if (missing) { error.value = t('credentialRequest.required', { label: missing.label }); return }
  busy.value = true
  try {
    await personalCredentials.submit(id.value, Object.fromEntries(form.value.fields.map(f => [f.key, values[f.key]])))
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
    <h1>{{ t('credentialRequest.title') }}</h1>
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
      :title="t('credentialRequest.saved')"
      :sub-title="t('credentialRequest.savedHint')"
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
          <el-input
            v-model="values[field.key]"
            :data-test="'field-' + field.key"
            :type="field.secret ? 'password' : 'text'"
            :show-password="field.secret"
            :placeholder="field.placeholder"
            :maxlength="4096"
            autocomplete="off"
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
</style>
