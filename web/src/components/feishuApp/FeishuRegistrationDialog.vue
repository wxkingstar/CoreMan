<script setup lang="ts">
import QRCode from 'qrcode'
import { computed, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { feishuApps, type FeishuRegistration, type FeishuRegistrationIn } from '@/api/feishuApps'
import { errorMessage } from '@/utils/errors'

/**
 * 扫码创建（purpose=create）或补齐已绑定应用的权限（purpose=update）。
 * 凭证只在服务端流转：这里只拿到状态、确认链接与 App ID。
 */
const props = defineProps<{
  purpose: 'create' | 'update'
  botId?: string
  preset?: Pick<FeishuRegistrationIn, 'name' | 'description' | 'avatar_url'>
  /** 扫码完成后调用方还有工作（例如创建员工）时展示的忙碌文案。 */
  busy?: boolean
}>()
const visible = defineModel<boolean>('visible', { required: true })
const emit = defineEmits<{ succeeded: [FeishuRegistration] }>()
const { t } = useI18n()

const row = ref<FeishuRegistration | null>(null)
const qr = ref('')
const error = ref('')
const starting = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0

const finished = computed(() => row.value && ['expired', 'denied', 'failed', 'cancelled'].includes(row.value.status))
const reusedApp = computed(() => row.value?.reused && row.value.status === 'succeeded')

function stopPolling(): void {
  if (timer) clearTimeout(timer)
  timer = undefined
}

async function show(next: FeishuRegistration, current: number): Promise<void> {
  if (current !== generation) return
  row.value = next
  // 确认链接带着压缩后的权限清单（约 700 字符）：低纠错级别 + 更大尺寸，手机扫屏幕更容易识别。
  qr.value = next.url ? await QRCode.toDataURL(next.url, { width: 280, margin: 1, errorCorrectionLevel: 'L' }) : ''
  stopPolling()
  if (next.status === 'pending') {
    timer = setTimeout(() => void poll(current), Math.max(1, next.retry_after ?? 3) * 1000)
    return
  }
  if (next.reused) return // 复用前先让本人确认
  if (next.status === 'succeeded' || next.status === 'consumed') emit('succeeded', next)
}

async function start(reuse = true): Promise<void> {
  const current = ++generation
  stopPolling()
  starting.value = true
  error.value = ''
  row.value = null
  qr.value = ''
  try {
    const body: FeishuRegistrationIn = props.purpose === 'create'
      ? { purpose: 'create', reuse, ...props.preset }
      : { purpose: 'update', bot_id: props.botId }
    await show(await feishuApps.startRegistration(body), current)
  } catch (e) {
    if (current === generation) error.value = errorMessage(e)
  } finally {
    if (current === generation) starting.value = false
  }
}

async function poll(current: number): Promise<void> {
  if (current !== generation || !row.value) return
  try {
    await show(await feishuApps.registration(row.value.id), current)
  } catch (e) {
    if (current !== generation) return
    // 临时网络错误不打断扫码：提示后按原节奏继续。
    error.value = errorMessage(e)
    timer = setTimeout(() => void poll(current), 5000)
  }
}

function useReused(): void {
  if (row.value) emit('succeeded', { ...row.value, reused: false })
}

async function close(): Promise<void> {
  const pending = row.value?.status === 'pending' ? row.value.id : null
  ++generation
  stopPolling()
  visible.value = false
  if (pending) {
    try { await feishuApps.cancelRegistration(pending) } catch { /* 过期后自然失效 */ }
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
    :title="purpose === 'create' ? t('feishuApp.scanCreateTitle') : t('feishuApp.scanUpdateTitle')"
    width="440px"
    :close-on-click-modal="false"
    append-to-body
    data-test="feishu-registration"
    @close="close"
  >
    <div
      v-loading="starting || busy"
      class="registration"
      :element-loading-text="busy ? t('feishuApp.creatingEmployee') : undefined"
    >
      <template v-if="reusedApp && row">
        <el-alert
          type="info"
          :closable="false"
          :title="t('feishuApp.reuseTitle', { appId: row.app_id })"
          :description="t('feishuApp.reuseHint')"
        />
        <div class="actions">
          <el-button
            data-test="registration-new"
            @click="start(false)"
          >
            {{ t('feishuApp.scanNew') }}
          </el-button>
          <el-button
            type="primary"
            data-test="registration-reuse"
            @click="useReused"
          >
            {{ t('feishuApp.useReused') }}
          </el-button>
        </div>
      </template>
      <template v-else-if="row?.status === 'pending'">
        <p class="hint">
          {{ purpose === 'create' ? t('feishuApp.scanCreateHint') : t('feishuApp.scanUpdateHint') }}
        </p>
        <img
          v-if="qr"
          :src="qr"
          class="qr"
          :alt="t('feishuApp.qrAlt')"
          data-test="registration-qr"
        >
        <a
          v-if="row.url"
          :href="row.url"
          target="_blank"
          rel="noopener noreferrer"
          data-test="registration-link"
        >{{ t('feishuApp.openInFeishu') }}</a>
        <p class="muted">
          {{ t('feishuApp.waiting') }}
        </p>
      </template>
      <template v-else-if="row && (row.status === 'succeeded' || row.status === 'consumed')">
        <el-result
          icon="success"
          :title="purpose === 'create' ? t('feishuApp.created', { appId: row.app_id }) : t('feishuApp.updated')"
        />
      </template>
      <template v-else-if="finished && row">
        <el-result
          icon="warning"
          :title="t(`feishuApp.registrationStatus.${row.status}`)"
          :sub-title="row.error ? t(`feishuApp.registrationError.${row.error}`, row.error) : ''"
        >
          <template #extra>
            <el-button
              type="primary"
              data-test="registration-retry"
              @click="start(false)"
            >
              {{ t('feishuApp.retry') }}
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
.registration { display: flex; flex-direction: column; align-items: center; gap: 12px; min-height: 160px; text-align: center; }
.qr { width: 280px; height: 280px; max-width: 100%; }
.hint { margin: 0; line-height: 1.6; }
.muted { margin: 0; color: var(--el-text-color-secondary); font-size: 12px; }
.actions { display: flex; gap: 8px; justify-content: center; }
</style>
