<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { auth as authApi } from '@/api/admin'
import { getLocale } from '@/i18n'

export type ScanPlatform = 'feishu' | 'wecom'

const props = defineProps<{ platform: ScanPlatform; redirect: string }>()
const emit = defineEmits<{ account: []; password: [] }>()
const { t } = useI18n()

// 飞书扫码 SDK（LarkSSOSDKWebQRCode）只做两件事：建这个 iframe、收它 postMessage 回来的 tmp_code。
// 这里直接实现，不加载第三方脚本；收到 tmp_code 后整页跳到 goto&tmp_code，飞书再带 code 回 callback。
const FEISHU_QR_PAGE = 'https://passport.feishu.cn/suite/passport/sso/qr'
const FEISHU_ORIGIN = /^https:\/\/([\w-]+\.)*feishu\.cn$/
// 后端 state 10 分钟过期，二维码提前换新。
const REFRESH_MS = 9 * 60 * 1000

const status = ref<'loading' | 'waiting' | 'confirmed' | 'failed'>('loading')
const feishuGoto = ref('')
const feishuFrame = ref<HTMLIFrameElement>()
const wecomHost = ref<HTMLElement>()
let wecomPanel: { unmount(): void } | null = null
let refreshTimer: number | undefined
let alive = true

const platformName = computed(() => t('login.platforms.' + props.platform))
const icon = computed(() => `/platforms/${props.platform}.svg`)
const feishuSrc = computed(() =>
  feishuGoto.value ? `${FEISHU_QR_PAGE}?goto=${encodeURIComponent(feishuGoto.value)}&sdk_version=1.0.3` : '')

async function mountWecom() {
  const [params, sdk] = await Promise.all([authApi.wecomQr(props.redirect), import('@wecom/jssdk')])
  if (!alive || !wecomHost.value) return
  wecomPanel?.unmount()
  wecomPanel = sdk.createWWLoginPanel({
    el: wecomHost.value,
    params: {
      ...params,
      login_type: sdk.WWLoginType.corpApp,
      redirect_type: sdk.WWLoginRedirectType.callback,
      panel_size: sdk.WWLoginPanelSizeType.small,
      lang: getLocale() === 'zh' ? sdk.WWLoginLangType.zh : sdk.WWLoginLangType.en,
      // 组件 iframe 背景透明，深色主题下要让它用浅色文字。
      color_scheme: document.documentElement.classList.contains('dark') ? sdk.ColorScheme.Dark : sdk.ColorScheme.Light,
    },
    onLoginSuccess: ({ code }) => {
      status.value = 'confirmed'
      window.location.assign(`/api/auth/wecom/callback?${new URLSearchParams({ code, state: params.state })}`)
    },
  })
}

async function load() {
  window.clearTimeout(refreshTimer)
  status.value = 'loading'
  try {
    if (props.platform === 'feishu') feishuGoto.value = (await authApi.feishuQr(props.redirect)).goto
    else await mountWecom()
    if (!alive) return
    status.value = 'waiting'
    refreshTimer = window.setTimeout(load, REFRESH_MS)
  } catch {
    if (alive) status.value = 'failed'
  }
}

function onMessage(e: MessageEvent) {
  if (props.platform !== 'feishu' || !feishuGoto.value || status.value === 'confirmed') return
  if (e.source !== feishuFrame.value?.contentWindow || !FEISHU_ORIGIN.test(e.origin)) return
  const data = e.data as { source?: unknown; tmp_code?: unknown } | null
  if (data?.source !== 'qrcode' || typeof data.tmp_code !== 'string' || !data.tmp_code) return
  status.value = 'confirmed'
  window.location.assign(`${feishuGoto.value}&tmp_code=${encodeURIComponent(data.tmp_code)}`)
}

onMounted(() => {
  window.addEventListener('message', onMessage)
  void load()
})

onBeforeUnmount(() => {
  alive = false
  window.clearTimeout(refreshTimer)
  window.removeEventListener('message', onMessage)
  wecomPanel?.unmount()
})
</script>

<template>
  <div class="login-scan">
    <div class="scan-head">
      <img
        :src="icon"
        alt=""
        width="30"
        height="30"
      ><h2>{{ t('login.scanTitle', { platform: platformName }) }}</h2>
    </div>
    <p class="scan-hint">
      {{ t('login.scanHint', { platform: platformName }) }}
    </p>
    <div
      class="scan-code"
      :class="platform"
    >
      <iframe
        v-if="platform === 'feishu' && feishuSrc"
        ref="feishuFrame"
        :key="feishuSrc"
        :src="feishuSrc"
        :title="t('login.scanTitle', { platform: platformName })"
        data-test="feishu-qr"
      />
      <div
        v-if="platform === 'wecom'"
        ref="wecomHost"
        class="wecom-host"
        data-test="wecom-qr"
      />
      <div
        v-if="status === 'failed'"
        class="scan-failed"
      >
        <span>{{ t('login.scanFailed') }}</span>
        <el-button
          link
          type="primary"
          data-test="scan-retry"
          @click="load"
        >
          {{ t('login.scanRetry') }}
        </el-button>
      </div>
    </div>
    <p
      v-if="platform === 'feishu' && status !== 'failed'"
      class="scan-status"
      :class="status"
      data-test="scan-status"
    >
      <span class="dot" />{{ status === 'confirmed' ? t('login.scanConfirmed') : t('login.scanWaiting') }}
    </p>
    <el-divider>{{ t('login.or') }}</el-divider>
    <el-button
      class="scan-account"
      size="large"
      data-test="account"
      @click="emit('account')"
    >
      <img
        :src="icon"
        alt=""
        width="18"
        height="18"
      >{{ t('login.useAccount', { platform: platformName }) }}
    </el-button>
    <p class="scan-note">
      {{ t('login.accountHint.' + platform) }}
    </p>
    <div class="scan-password">
      <el-button
        link
        type="primary"
        data-test="use-password"
        @click="emit('password')"
      >
        {{ t('login.usePassword', { platform: platformName }) }}
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.login-scan { display: flex; flex-direction: column; align-items: center; text-align: center; }
.scan-head { display: flex; align-items: center; gap: 10px; align-self: flex-start; }
.scan-head h2 { font-size: 26px; font-weight: 650; letter-spacing: -0.5px; margin: 0; }
.scan-hint { align-self: flex-start; color: var(--cm-muted); font-size: 14px; margin: 10px 0 22px; text-align: left; }
.scan-code { position: relative; display: grid; place-items: center; background: #fff; border: 1px solid var(--el-border-color-lighter); border-radius: 16px; overflow: hidden; }
.scan-code.feishu { width: 272px; height: 272px; }
/* 组件页面里二维码四周留白较多，iframe 比外框大一圈、居中裁掉多余的白边。 */
.scan-code.feishu iframe { position: absolute; left: calc(50% - 150px); top: calc(50% - 150px); width: 300px; height: 300px; border: 0; }
.scan-code.wecom { width: 320px; min-height: 380px; border: 0; background: transparent; }
.wecom-host { display: flex; justify-content: center; }
.scan-failed { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; background: var(--el-bg-color); color: var(--cm-muted); font-size: 13px; }
.scan-status { display: flex; align-items: center; gap: 8px; color: var(--cm-muted); font-size: 13px; margin: 16px 0 0; }
.scan-status .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--el-color-warning); }
.scan-status.confirmed .dot { background: var(--el-color-success); }
.login-scan :deep(.el-divider) { margin: 26px 0 22px; }
.login-scan :deep(.el-divider__text) { color: var(--cm-muted); font-weight: 400; }
.scan-account { width: 100%; }
.scan-account img { margin-right: 8px; }
.scan-note { color: var(--cm-muted); font-size: 13px; margin: 12px 0 0; }
.scan-password { margin-top: 18px; }
</style>
