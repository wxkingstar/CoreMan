<script setup lang="ts">
import QRCode from 'qrcode'
import { computed, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { wecomBots, type WecomProvision } from '@/api/wecomBots'
import { errorMessage } from '@/utils/errors'

/**
 * 扫码创建企业微信智能机器人。二维码本身就能取回机器人 Secret，所以只画给发起人本人看，
 * 不提供可转发的链接；取回、校验、交付都在服务端完成，这里只拿到状态与 Bot ID。
 */
defineProps<{
  /** 扫码完成后调用方还有工作（创建员工）时展示的忙碌文案。 */
  busy?: boolean
}>()
const visible = defineModel<boolean>('visible', { required: true })
const emit = defineEmits<{ succeeded: [WecomProvision] }>()
const { t } = useI18n()

const row = ref<WecomProvision | null>(null)
const qr = ref('')
const error = ref('')
const starting = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0

const finished = computed(() => row.value && ['expired', 'failed', 'cancelled'].includes(row.value.status))
const delivered = computed(() => row.value && ['succeeded', 'consumed'].includes(row.value.status))
const reusedBot = computed(() => row.value?.reused && row.value.status === 'succeeded')
/** 企业微信在扫码后会从 init 变成别的状态；伪造的会话也会返回 pending，所以只作提示。 */
const scanned = computed(() => !!row.value?.upstream_status && row.value.upstream_status !== 'init')
const expiresAt = computed(() => (row.value ? new Date(row.value.expires_at).toLocaleTimeString() : ''))

function stopPolling(): void {
  if (timer) clearTimeout(timer)
  timer = undefined
}

async function show(next: WecomProvision, current: number): Promise<void> {
  if (current !== generation) return
  row.value = next
  qr.value = next.url ? await QRCode.toDataURL(next.url, { width: 240, margin: 1, errorCorrectionLevel: 'M' }) : ''
  stopPolling()
  if (next.status === 'pending') {
    timer = setTimeout(() => void poll(current), Math.max(1, next.retry_after ?? 3) * 1000)
    return
  }
  if (next.reused) return // 复用前先让本人确认
  // 校验没通过时停下来说明原因，由本人处理后再继续。
  if (next.status === 'succeeded' && next.verified) emit('succeeded', next)
}

async function start(reuse = true): Promise<void> {
  const current = ++generation
  stopPolling()
  starting.value = true
  error.value = ''
  row.value = null
  qr.value = ''
  try {
    await show(await wecomBots.startProvision({ reuse }), current)
  } catch (e) {
    if (current === generation) error.value = errorMessage(e)
  } finally {
    if (current === generation) starting.value = false
  }
}

async function poll(current: number): Promise<void> {
  if (current !== generation || !row.value) return
  try {
    await show(await wecomBots.provision(row.value.id), current)
  } catch (e) {
    if (current !== generation) return
    // 临时网络错误不打断扫码：提示后按原节奏继续，二维码有效期由服务端计时。
    error.value = errorMessage(e)
    timer = setTimeout(() => void poll(current), 5000)
  }
}

function useBot(): void {
  if (row.value) emit('succeeded', { ...row.value, reused: false })
}

async function close(): Promise<void> {
  const pending = row.value?.status === 'pending' ? row.value.id : null
  ++generation
  stopPolling()
  visible.value = false
  if (pending) {
    // 服务端取消前会再查一次：刚扫完码就关窗口，建好的机器人会留到下次复用。
    try { await wecomBots.cancelProvision(pending) } catch { /* 过期后自然失效 */ }
  }
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
    :title="t('wecomBot.scanCreateTitle')"
    width="440px"
    :close-on-click-modal="false"
    append-to-body
    data-test="wecom-provision"
    @close="close"
  >
    <div
      v-loading="starting || busy"
      class="provision"
      :element-loading-text="busy ? t('wecomBot.creatingEmployee') : undefined"
    >
      <template v-if="reusedBot && row">
        <el-alert
          type="info"
          :closable="false"
          :title="t('wecomBot.reuseTitle', { botId: row.wecom_bot_id })"
          :description="t('wecomBot.reuseHint')"
        />
        <div class="actions">
          <el-button
            data-test="provision-new"
            @click="start(false)"
          >
            {{ t('wecomBot.scanNew') }}
          </el-button>
          <el-button
            type="primary"
            data-test="provision-reuse"
            @click="useBot"
          >
            {{ t('wecomBot.useReused') }}
          </el-button>
        </div>
      </template>
      <template v-else-if="row?.status === 'pending'">
        <p class="hint">
          {{ t('wecomBot.scanHint') }}
        </p>
        <img
          v-if="qr"
          :src="qr"
          class="qr"
          :alt="t('wecomBot.qrAlt')"
          data-test="provision-qr"
        >
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          :title="t('wecomBot.qrIsSecret')"
          data-test="provision-secret-warning"
        />
        <el-alert
          v-if="row.error === 'unexpected_response'"
          type="error"
          :closable="false"
          :title="t('wecomBot.provisionError.unexpected_response')"
          data-test="provision-upstream-changed"
        />
        <p
          class="muted"
          data-test="provision-progress"
        >
          {{ scanned ? t('wecomBot.scanned') : t('wecomBot.waiting', { time: expiresAt }) }}
        </p>
        <p class="muted">
          {{ t('wecomBot.permissionHint') }}
        </p>
      </template>
      <template v-else-if="row && delivered && !row.verified">
        <el-result
          icon="warning"
          :title="t('wecomBot.verifyFailedTitle', { botId: row.wecom_bot_id })"
          :sub-title="t(`wecomBot.provisionError.${row.error ?? 'verify_rejected'}`, t('wecomBot.provisionError.verify_rejected'))"
        >
          <template #extra>
            <el-button
              type="primary"
              data-test="provision-verify"
              @click="useBot"
            >
              {{ t('wecomBot.verifyAndCreate') }}
            </el-button>
          </template>
        </el-result>
      </template>
      <template v-else-if="row && delivered">
        <el-result
          icon="success"
          :title="t('wecomBot.created', { botId: row.wecom_bot_id })"
          :sub-title="t('wecomBot.permissionHint')"
        />
      </template>
      <template v-else-if="finished && row">
        <el-result
          icon="warning"
          :title="t(`wecomBot.provisionStatus.${row.status}`)"
          :sub-title="row.error ? t(`wecomBot.provisionError.${row.error}`, row.error) : ''"
        >
          <template #extra>
            <el-button
              type="primary"
              data-test="provision-retry"
              @click="start(false)"
            >
              {{ t('wecomBot.retry') }}
            </el-button>
          </template>
        </el-result>
      </template>
      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        :title="error"
      />
    </div>
  </el-dialog>
</template>

<style scoped>
.provision { display: flex; flex-direction: column; align-items: center; gap: 12px; min-height: 160px; text-align: center; }
.qr { width: 240px; height: 240px; max-width: 100%; }
.hint { margin: 0; line-height: 1.6; }
.muted { margin: 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.actions { display: flex; gap: 8px; justify-content: center; }
</style>
