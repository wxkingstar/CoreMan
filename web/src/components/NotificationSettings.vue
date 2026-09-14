<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { smtp, type SmtpIn } from '@/api/cron'
const { t } = useI18n()
const form = reactive<SmtpIn>({ enabled: false, host: '', port: 465, security: 'tls', username: '', sender: '' })
const version = ref(0), password = ref(''), hasPassword = ref(false), loading = ref(false), loaded = ref(false)
function fail(e: unknown) { ElMessage.error(e instanceof Error ? e.message : String(e)) }
onMounted(async () => {
  try {
    const data = await smtp.get()
    for (const key of Object.keys(form) as (keyof SmtpIn)[]) Object.assign(form, { [key]: data[key] })
    version.value = data.version; hasPassword.value = data.has_password; loaded.value = true
  } catch (e) { fail(e) }
})
async function save() {
  loading.value = true
  try {
    const result = await smtp.save({ ...form, ...(password.value ? { password: password.value } : {}) }, version.value)
    version.value = result.version; hasPassword.value = result.has_password; password.value = ''
    ElMessage.success(t('common.saved'))
  } catch (e) { fail(e) } finally { loading.value = false }
}
</script>
<template>
  <el-card class="notification-settings">
    <template #header>
      {{ t('smtp.title') }}
    </template>
    <p>{{ t('smtp.hint') }}</p>
    <el-form
      label-position="top"
      :disabled="!loaded"
      @submit.prevent="save"
    >
      <el-form-item :label="t('common.enable')">
        <el-switch v-model="form.enabled" />
      </el-form-item>
      <div class="smtp-grid">
        <el-form-item :label="t('smtp.host')">
          <el-input
            v-model="form.host"
            autocomplete="off"
          />
        </el-form-item>
        <el-form-item :label="t('smtp.port')">
          <el-input-number
            v-model="form.port"
            :min="1"
            :max="65535"
          />
        </el-form-item>
        <el-form-item :label="t('smtp.security')">
          <el-select v-model="form.security">
            <el-option
              label="TLS"
              value="tls"
            /><el-option
              label="STARTTLS"
              value="starttls"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('smtp.sender')">
          <el-input
            v-model="form.sender"
            type="email"
          />
        </el-form-item>
        <el-form-item :label="t('smtp.username')">
          <el-input
            v-model="form.username"
            autocomplete="off"
          />
        </el-form-item>
        <el-form-item :label="t('smtp.password')">
          <el-input
            v-model="password"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="t(hasPassword ? 'smtp.saved' : 'smtp.unset')"
          />
        </el-form-item>
      </div>
      <el-button
        :loading="loading"
        type="primary"
        data-test="save-smtp"
        @click="save"
      >
        {{ t('common.save') }}
      </el-button>
    </el-form>
  </el-card>
</template>
<style scoped>
.notification-settings { margin-top: 24px; }
.smtp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
p { color: var(--el-text-color-secondary); font-size: 13px; }
@media (max-width: 600px) { .smtp-grid { grid-template-columns: 1fr; } }
</style>
