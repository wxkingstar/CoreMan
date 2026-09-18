<script setup lang="ts">
import BrandLogo from '@/components/BrandLogo.vue'
import LoginCompanions from '@/components/LoginCompanions.vue'
import { Moon } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { auth as authApi } from '@/api/admin'
import type { Providers } from '@/api/types'
import { getLocale, setLocale, type Locale } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

const ERROR_KEYS = [
  'invalid_state', 'code_replayed', 'wecom_error', 'feishu_error', 'feishu_not_configured', 'not_member',
  'user_not_found', 'user_disabled', 'rate_limited', 'wecom_not_configured',
] as const

const { t } = useI18n()
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const formRef = ref<FormInstance>()
const dark = ref(document.documentElement.classList.contains('dark'))
function toggleDark() { dark.value = !dark.value; document.documentElement.classList.toggle('dark', dark.value); try { localStorage.setItem('coreman.dark', dark.value ? '1' : '0') } catch { /* storage unavailable */ } }
const passwordFocused = ref(false)
const loading = ref(false)
const form = reactive({ username: '', password: '' })
const locale = ref<Locale>(getLocale())
const providers = ref<Providers>({ wecom: false, feishu: false })

const errorKey = computed(() => {
  const key = route.query.error
  return typeof key === 'string' && (ERROR_KEYS as readonly string[]).includes(key) ? key : null
})
const errorMessage = computed(() => (errorKey.value ? t('login.errors.' + errorKey.value) : ''))

const rules = computed<FormRules>(() => ({
  username: [{ required: true, message: t('login.required', { field: t('login.username') }), trigger: 'blur' }],
  password: [{ required: true, message: t('login.required', { field: t('login.password') }), trigger: 'blur' }],
}))

function wecomRedirect(): string {
  const r = route.query.redirect
  return typeof r === 'string' && r.startsWith('/') ? r : '/'
}

function goWecom(mode: 'qr' | 'oauth') {
  window.location.assign(`/api/auth/wecom/start?mode=${mode}&redirect=${encodeURIComponent(wecomRedirect())}`)
}

function goFeishu() {
  window.location.assign(`/api/auth/feishu/start?redirect=${encodeURIComponent(wecomRedirect())}`)
}

onMounted(async () => {
  try {
    providers.value = await authApi.providers()
  } catch {
    providers.value = { wecom: false, feishu: false }
  }
  if (errorKey.value) return
  // 在企微或飞书客户端里打开（常见于点聊天里的会话链接）时直接走对应的免扫码登录。
  if (/wxwork/i.test(navigator.userAgent) && providers.value.wecom) goWecom('oauth')
  else if (/Lark|Feishu/i.test(navigator.userAgent) && providers.value.feishu) goFeishu()
})

async function submit() {
  if (loading.value) return // 回车提交不经过按钮的 loading 态，需要自己防重入
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.loginBootstrap(form.username, form.password)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    // 会话查看页由后端直接渲染，不在前端路由里，只能整页跳过去。
    if (redirect.startsWith('/api/')) window.location.assign(redirect)
    else await router.push(redirect)
  } catch (e) {
    ElMessage.error(e instanceof Error && e.message ? e.message : t('login.failed'))
  } finally {
    loading.value = false
  }
}

function changeLocale(l: Locale) {
  setLocale(l)
}
</script>

<template>
  <div class="login-page">
    <section class="login-story">
      <div class="story-brand">
        <BrandLogo :size="46" /><span>CoreMan</span>
      </div>
      <div class="story-copy">
        <h1>{{ t('login.artTitle') }}</h1><p>{{ t('login.artSubtitle') }}</p>
      </div>
      <LoginCompanions :covering="passwordFocused" />
      <div class="story-footer">
        <span>{{ t('login.artFooter') }}</span><span class="story-spark">✳</span>
      </div>
    </section>
    <section class="login-entry">
      <div class="login-tools">
        <el-button
          :icon="Moon"
          :aria-label="t('workspace.theme')"
          :aria-pressed="dark"
          @click="toggleDark"
        /><el-select
          v-model="locale"
          :aria-label="t('workspace.language')"
          style="width: 110px"
          @change="changeLocale"
        >
          <el-option
            label="中文"
            value="zh"
          /><el-option
            label="日本語"
            value="ja"
          /><el-option
            label="English"
            value="en"
          />
        </el-select>
      </div>
      <el-card class="login-card cm-controls-large">
        <template #header>
          <div class="login-header">
            <BrandLogo :size="42" /><h2 class="login-title">
              {{ t('login.title') }}
            </h2><p>{{ t('login.entryHint') }}</p>
          </div>
        </template>
        <el-alert
          v-if="errorKey"
          type="error"
          :title="errorMessage"
          :closable="false"
          class="login-alert"
        />
        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          @submit.prevent
        >
          <el-form-item
            :label="t('login.username')"
            prop="username"
          >
            <el-input
              v-model="form.username"
              data-test="username"
              autocomplete="username"
            />
          </el-form-item>
          <el-form-item
            :label="t('login.password')"
            prop="password"
          >
            <el-input
              v-model="form.password"
              data-test="password"
              type="password"
              show-password
              autocomplete="current-password"
              @focus="passwordFocused = true"
              @blur="passwordFocused = false"
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-button
            type="primary"
            native-type="button"
            :loading="loading"
            data-test="submit"
            style="width: 100%"
            @click="submit"
          >
            {{ t('login.submit') }}
          </el-button>
        </el-form>
        <p class="hint">
          {{ t('login.bootstrapHint') }}
        </p>
        <el-divider />
        <div class="platforms">
          <el-tooltip
            :content="t('login.wecomHint')"
            :disabled="providers.wecom"
          >
            <span><el-button
              :disabled="!providers.wecom"
              data-test="wecom"
              @click="goWecom('qr')"
            >{{ t('login.wecom') }}</el-button></span>
          </el-tooltip>
          <el-tooltip
            :disabled="providers.feishu"
            :content="t('login.errors.feishu_not_configured')"
          >
            <span><el-button
              :disabled="!providers.feishu"
              data-test="feishu"
              @click="goFeishu"
            >{{ t('login.feishu') }}</el-button></span>
          </el-tooltip>
        </div>
      </el-card>
      <p class="entry-footer">
        {{ t('login.entryFooter') }}
      </p>
    </section>
  </div>
</template>

<style scoped>
.login-page { min-height: 100svh; display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(400px, 1fr); background: var(--cm-surface); }
.login-story { position: relative; overflow: hidden; background: #E6EDE3; color: #21483F; padding: 42px 48px 24px; display: flex; flex-direction: column; min-height: 100svh; }
.story-brand { display: flex; align-items: center; gap: 13px; font-size: 27px; font-weight: 720; letter-spacing: -1px; }.story-copy { margin: 58px 0 0; z-index: 1; }.story-copy h1 { font-size: clamp(34px, 3.8vw, 62px); line-height: 1.17; letter-spacing: -2px; white-space: pre-line; font-weight: 650; margin: 0 0 20px; }.story-copy p { color: #506C60; font-size: 15px; max-width: 34em; }
.companion-scene { margin-top: auto; margin-bottom: auto; }.story-footer { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #506C60; }.story-spark { font-size: 30px; }
.login-entry { position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 100px 48px 64px; min-width: 0; }.login-tools { position: absolute; top: 32px; right: 36px; display: flex; gap: 10px; }.login-tools .el-button { margin: 0; }
.login-card { width: 380px; max-width: 100%; border: 0; background: transparent; overflow: visible; }.login-card :deep(.el-card__body), .login-card :deep(.el-card__header) { padding: 0; border: 0; }.login-card :deep(.el-card__header) { margin-bottom: 34px; }.login-header { display: flex; flex-direction: column; align-items: flex-start; gap: 12px; }.login-title { font-size: 29px; font-weight: 650; margin: 12px 0 0; letter-spacing: -1px; }.login-header p { color: var(--cm-muted); font-size: 14px; margin: 0; }.login-header .coreman-logo { display: none; }
.login-card :deep(.el-form-item) { margin-bottom: 24px; }.login-card :deep(.el-form-item__label) { padding-bottom: 9px; }.login-alert { margin-bottom: 20px; }.hint { color: var(--cm-muted); font-size: 12px; margin: 18px 0 0; }.platforms { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }.platforms .el-button { width: 100%; }.entry-footer { position: absolute; bottom: 25px; font-size: 12px; color: var(--cm-muted); margin: 0; }
:global(html.dark) .login-story { background: #243F38; color: #E0EED9; }:global(html.dark) .story-copy p, :global(html.dark) .story-footer { color: #B3CBB8; }
@media(min-width:1600px) { .login-story { padding: 48px 70px 30px; }.story-copy { margin-top: 80px; } }
@media(max-width:1023px) { .login-page { grid-template-columns: minmax(0,1fr) minmax(360px,1fr); }.login-story { padding: 32px 24px 24px; }.story-copy { margin-top: 70px; }.story-copy h1 { font-size: 38px; }.login-entry { padding: 100px 30px 64px; } }
@media(max-width:767px) { .login-page { grid-template-columns: 1fr; }.login-story { min-height: auto; padding: 24px 24px 0; }.story-brand { font-size: 22px; }.story-brand :deep(img) { width: 36px; height: 36px; }.story-copy { margin: 26px 0 0; }.story-copy h1 { font-size: 33px; white-space: pre-line; letter-spacing: -1px; max-width: 12em; }.story-copy p { font-size: 13px; margin-bottom: 0; }.companion-scene { max-width: 430px; margin-top: -6px; }.story-footer { display: none; }.login-entry { padding: 88px 24px 64px; }.login-tools { top: 22px; right: 24px; }.login-card :deep(.el-card__header) { margin-bottom: 28px; }.login-title { font-size: 26px; margin-top: 0; } }
</style>
