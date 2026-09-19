<script setup lang="ts">
import QRCode from 'qrcode'
import { computed, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { wecomBinding, type WecomBinding } from '@/api/wecomBinding'
import { errorMessage } from '@/utils/errors'

/**
 * 扫码绑定本人的企业微信授权机器人。二维码内容等同于授权机器人的密钥（拿到的人能取回 Secret），
 * 所以只画给本人看；唯一的链接留给在手机企业微信里打开本页的人直接跳转。
 * 轮询、核对本人、保存凭证都在服务端完成，这里只展示进度。
 */
const props = defineProps<{
  /** 手机企业微信里从私聊链接进来：拿到二维码内容就直接跳过去，由企业微信打开确认页。 */
  redirect?: boolean
}>()
const visible = defineModel<boolean>('visible', { required: true })
const emit = defineEmits<{ changed: [WecomBinding]; bound: [WecomBinding] }>()
const { t, te } = useI18n()

const row = ref<WecomBinding | null>(null)
const qr = ref('')
const error = ref('')
const starting = ref(false)
const redirected = ref(false)
let qrSource: string | null = null
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0

const scan = computed(() => row.value?.scan ?? null)
const pending = computed(() => scan.value?.status === 'pending')
/** 企业微信在扫码后会从 init 变成别的状态；只作提示。 */
const scanned = computed(() => !!scan.value?.upstream_status && scan.value.upstream_status !== 'init')
/** 已经拿到授权机器人的凭证，正在向企业微信核对是不是本人。 */
const verifying = computed(() => scan.value?.upstream_status === 'success')
const expiresAt = computed(() => (scan.value?.expires_at ? new Date(scan.value.expires_at).toLocaleTimeString() : ''))
const succeeded = (next: WecomBinding) => next.status === 'bound' && next.scan?.status === 'succeeded'
const failed = computed(() => !starting.value && !pending.value && (row.value ? !succeeded(row.value) : !!error.value))
const failureTitle = computed(() => {
  const status = scan.value?.status
  return t(`myWecom.scan.status.${status === 'expired' || status === 'cancelled' ? status : 'failed'}`)
})
const failureReason = computed(() => {
  if (!row.value) return error.value
  const key = `myWecom.scan.error.${scan.value?.error}`
  return scan.value?.error && te(key) ? t(key) : t('myWecom.scan.error.unknown')
})

function stopPolling(): void {
  if (timer) clearTimeout(timer)
  timer = undefined
}

async function show(next: WecomBinding, current: number): Promise<void> {
  if (current !== generation) return
  row.value = next
  emit('changed', next)
  const source = next.scan?.status === 'pending' ? next.scan.url : null
  if (source !== qrSource) {
    qrSource = source
    qr.value = source ? await QRCode.toDataURL(source, { width: 240, margin: 1, errorCorrectionLevel: 'M' }) : ''
  }
  if (current !== generation) return
  stopPolling()
  if (props.redirect && source && !redirected.value) {
    // 用 replace：本人在企业微信里确认完按返回，直接回到私聊等通知。
    redirected.value = true
    window.location.replace(source)
    return
  }
  if (next.scan?.status === 'pending') {
    timer = setTimeout(() => void poll(current), Math.max(1, next.scan.retry_after ?? 3) * 1000)
    return
  }
  if (succeeded(next)) {
    visible.value = false
    emit('bound', next)
  }
}

async function start(): Promise<void> {
  const current = ++generation
  stopPolling()
  starting.value = true
  error.value = ''
  row.value = null
  qr.value = ''
  qrSource = null
  try {
    await show(await wecomBinding.startScan(), current)
  } catch (e) {
    if (current === generation) error.value = errorMessage(e)
  } finally {
    if (current === generation) starting.value = false
  }
}

async function poll(current: number): Promise<void> {
  if (current !== generation) return
  try {
    const next = await wecomBinding.pollScan()
    if (current === generation) error.value = ''
    await show(next, current)
  } catch (e) {
    if (current !== generation) return
    // 临时网络错误不打断扫码：提示后稍后再查，二维码有效期由服务端计时。
    error.value = errorMessage(e)
    timer = setTimeout(() => void poll(current), 5000)
  }
}

async function close(): Promise<void> {
  const wasPending = pending.value
  ++generation
  stopPolling()
  visible.value = false
  if (!wasPending) return
  try {
    // 服务端取消前会再查一次：刚扫完码就关窗口，已经建好的授权机器人仍会绑定上。
    const next = await wecomBinding.cancelScan()
    emit('changed', next)
    if (succeeded(next)) emit('bound', next)
  } catch { /* 过期后自然失效 */ }
}

watch(visible, (open) => {
  if (open) void start()
  else { ++generation; stopPolling() }
}, { immediate: true })
onUnmounted(() => { ++generation; stopPolling() })
</script>

<template>
  <el-dialog
    :model-value="visible"
    :title="t('myWecom.scan.title')"
    width="460px"
    :close-on-click-modal="false"
    append-to-body
    data-test="wecom-bind"
    @close="close"
  >
    <div
      v-loading="starting"
      class="bind"
    >
      <p
        v-if="redirected"
        class="hint"
        data-test="bind-redirecting"
      >
        {{ t('myWecom.scan.redirecting') }}
      </p>
      <template v-else-if="pending && scan">
        <p class="hint">
          {{ t('myWecom.scan.hint') }}
        </p>
        <img
          v-if="qr"
          :src="qr"
          class="qr"
          :alt="t('myWecom.scan.qrAlt')"
          data-test="bind-qr"
        >
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          :title="t('myWecom.scan.secret')"
          data-test="bind-secret-warning"
        />
        <ol
          class="steps"
          data-test="bind-steps"
        >
          <li>{{ t('myWecom.scan.step1') }}</li>
          <li>{{ t('myWecom.scan.step2') }}</li>
          <li><strong>{{ t('myWecom.scan.step3') }}</strong></li>
        </ol>
        <p
          class="progress"
          data-test="bind-progress"
        >
          {{ verifying ? t('myWecom.scan.verifying') : scanned ? t('myWecom.scan.scanned') : t('myWecom.scan.waiting', { time: expiresAt }) }}
        </p>
        <el-alert
          v-if="scan.error || error"
          type="warning"
          :closable="false"
          :title="error || t('myWecom.scan.retrying')"
          data-test="bind-poll-error"
        />
        <div
          v-if="scan.url"
          class="open-in-wecom"
        >
          <a
            :href="scan.url"
            target="_blank"
            rel="noopener noreferrer"
            data-test="bind-open-in-wecom"
          >{{ t('myWecom.scan.openInWecom') }}</a>
          <span>{{ t('myWecom.scan.openInWecomHint') }}</span>
        </div>
      </template>
      <el-result
        v-else-if="failed"
        icon="warning"
        :title="failureTitle"
        :sub-title="failureReason"
        data-test="bind-failed"
      >
        <template #extra>
          <el-button
            type="primary"
            data-test="bind-retry"
            @click="start()"
          >
            {{ t('myWecom.scan.retry') }}
          </el-button>
        </template>
      </el-result>
    </div>
  </el-dialog>
</template>

<style scoped>
.bind { display: flex; flex-direction: column; align-items: center; gap: 12px; min-height: 160px; text-align: center; }
.qr { width: 240px; height: 240px; max-width: 100%; }
.hint { margin: 0; line-height: 1.6; }
.steps { align-self: stretch; margin: 0; padding-left: 1.6em; text-align: left; line-height: 1.8; }
.progress { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
.open-in-wecom { display: flex; flex-direction: column; gap: 2px; font-size: 12px; line-height: 1.6; color: var(--el-text-color-secondary); }
.open-in-wecom a { color: var(--el-color-primary); font-size: 13px; }
</style>
