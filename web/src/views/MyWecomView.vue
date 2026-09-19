<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import LoadState from '@/components/LoadState.vue'
import WecomBindDialog from '@/components/wecomPersonal/WecomBindDialog.vue'
import { wecomBinding, type Capability, type CapabilityKind, type WecomAuthorizationLevel, type WecomBinding } from '@/api/wecomBinding'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'
// 私聊里要原样发送的命令，不随界面语言翻译。
const CONNECT = '连接企业微信', DISCONNECT = '断开企业微信'
const LEVELS = ['readonly', 'all_except_send', 'all'] as const
const KINDS = ['read', 'write', 'send'] as const
const STATE_TAGS = { ok: 'success', unauthorized: 'warning', expired: 'danger', invalid: 'danger', unchecked: 'info' } as const
type ShownState = keyof typeof STATE_TAGS
// 刚绑定就全部未授权，多半是手机上跳过了「确认授权」：这段时间内提示，并自动重新检查一阵子。
const CONFIRM_WINDOW = 3 * 60_000, AUTO_CHECK_EVERY = 10_000, AUTO_CHECK_FOR = 2 * 60_000
const { t, te } = useI18n()
const binding = ref<WecomBinding | null>(null)
const loading = ref(false), error = ref(''), busy = ref(''), scanning = ref(false)
let requestVersion = 0
let refreshing = false, autoChecking = false
let timer: ReturnType<typeof setInterval> | undefined
let autoTimer: ReturnType<typeof setInterval> | undefined
let autoUntil = 0

const bound = computed(() => binding.value?.status === 'bound')
const days = computed(() => binding.value?.auth_ttl_days ?? 7)
const botLabel = computed(() => (bound.value && (binding.value?.bot_name || binding.value?.wecom_bot_id)) || t('myWecom.yourBot'))
const renewPath = computed(() => t('myWecom.renewPath', { bot: botLabel.value }))
const observed = computed(() => (binding.value?.capabilities ?? []).flatMap((c) => KINDS.map((k) => c[k]).filter((x): x is CapabilityKind => !!x)))
const awaitingConfirm = computed(() => {
  const b = binding.value
  if (!b || b.status !== 'bound' || !b.bound_at || Date.now() - Date.parse(b.bound_at) > CONFIRM_WINDOW) return false
  const reads = b.capabilities.map((c) => c.read).filter((x): x is CapabilityKind => !!x)
  return reads.length > 0 && reads.every((r) => r.state === 'unauthorized')
})
const needsRenew = computed(() => !awaitingConfirm.value && observed.value.some((k) => ['unauthorized', 'expired', 'invalid'].includes(k.state)))
const hasRenewUrl = computed(() => observed.value.some((k) => !!k.renew_url))

/** error、unknown 与从没观察到的一样，都算「未检测」。 */
function stateOf(kind: CapabilityKind | null): ShownState {
  return kind && kind.state in STATE_TAGS ? kind.state as ShownState : 'unchecked'
}
function renewUrl(cap: Capability): string | null {
  return KINDS.map((k) => cap[k]?.renew_url).find((url) => !!url) ?? null
}
/** 表格里只放得下「月-日 时:分」。 */
function shortTime(value: string): string {
  return formatDateTime(value).slice(5, 16)
}

/** 写操作的结果以服务端为准，并让之前发出的读取作废，免得旧状态盖掉新状态。 */
function apply(next: WecomBinding) {
  ++requestVersion
  binding.value = next
  loading.value = false
  error.value = ''
}
async function load(background = false) {
  if (background && (refreshing || loading.value || busy.value || scanning.value || autoChecking)) return
  const version = ++requestVersion
  if (background) refreshing = true
  else { loading.value = true; error.value = '' }
  try {
    const result = await wecomBinding.get()
    if (version === requestVersion) { binding.value = result; error.value = '' }
  } catch {
    if (version === requestVersion && !background) error.value = t('myWecom.loadError')
  } finally {
    if (background) refreshing = false
    if (version === requestVersion) loading.value = false
  }
}
function refreshVisible() {
  if (document.visibilityState !== 'hidden') void load(true)
}

async function run(action: string, task: () => Promise<WecomBinding>, done: string): Promise<void> {
  if (busy.value) return
  busy.value = action
  try {
    apply(await task())
    ElMessage.success(done)
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    busy.value = ''
  }
}
async function confirmed(message: string, title: string, type: 'warning' | 'info' = 'warning'): Promise<boolean> {
  if (busy.value) return false
  try {
    await ElMessageBox.confirm(message, title, { type })
    return true
  } catch {
    return false
  }
}
function setLevel(level: WecomAuthorizationLevel) {
  void run('level', () => wecomBinding.update({ authorization_level: level }), t('myWecom.saved'))
}
function setEnabled(enabled: boolean) {
  void run('enabled', () => wecomBinding.update({ enabled }), t(enabled ? 'myWecom.resumed' : 'myWecom.pausedDone'))
}
function check() {
  void run('check', () => wecomBinding.check(), t('myWecom.checked'))
}
async function renewed() {
  if (await confirmed(t('myWecom.confirmRenewed', { days: days.value }), t('myWecom.renewed'), 'info')) {
    await run('renewed', () => wecomBinding.renewed(), t('myWecom.renewedDone'))
  }
}
async function rebind() {
  if (await confirmed(t('myWecom.confirmRebind', { bot: botLabel.value }), t('myWecom.rebind'))) scanning.value = true
}
async function unbind() {
  const bot = botLabel.value
  if (await confirmed(t('myWecom.confirmUnbind', { bot }), t('myWecom.unbind'))) {
    await run('unbind', () => wecomBinding.unbind(), t('myWecom.unbindDone'))
  }
}
function onBound(next: WecomBinding) {
  apply(next)
  ElMessage.success(t('myWecom.boundSuccess'))
}

function stopAutoCheck() {
  clearInterval(autoTimer)
  autoTimer = undefined
}
async function autoCheck() {
  if (!awaitingConfirm.value || Date.now() > autoUntil) { stopAutoCheck(); return }
  if (autoChecking || busy.value || scanning.value || loading.value) return
  autoChecking = true
  const version = ++requestVersion
  try {
    const next = await wecomBinding.check()
    if (version === requestVersion) binding.value = next
  } catch { /* 下一轮再试 */ } finally {
    autoChecking = false
  }
}
watch(awaitingConfirm, (waiting) => {
  if (!waiting) { stopAutoCheck(); return }
  if (autoTimer) return
  autoUntil = Date.now() + AUTO_CHECK_FOR
  autoTimer = setInterval(() => void autoCheck(), AUTO_CHECK_EVERY)
})

onMounted(() => {
  void load()
  window.addEventListener('focus', refreshVisible)
  document.addEventListener('visibilitychange', refreshVisible)
  timer = setInterval(refreshVisible, 15000)
})
onUnmounted(() => {
  ++requestVersion
  clearInterval(timer)
  stopAutoCheck()
  window.removeEventListener('focus', refreshVisible)
  document.removeEventListener('visibilitychange', refreshVisible)
})
</script>
<template>
  <section>
    <header class="wecom-page-header">
      <div>
        <h2>{{ t('menu.myWecom') }}</h2>
        <p>{{ t('myWecom.intro') }}</p>
      </div>
      <el-button
        :loading="loading"
        :disabled="!!busy"
        @click="load()"
      >
        {{ t('myWecom.refresh') }}
      </el-button>
    </header>
    <div class="wecom-layout">
      <div class="wecom-main">
        <LoadState
          :loading="loading"
          :error="error"
          @retry="load()"
        />
        <template v-if="!loading && !error && binding">
          <article
            v-if="bound"
            class="wecom-binding"
            :class="{ 'is-enabled': binding.enabled }"
            data-test="binding"
          >
            <div class="wecom-binding-heading">
              <div class="wecom-bot-identity">
                <span
                  class="wecom-bot-avatar"
                  aria-hidden="true"
                >{{ botLabel.slice(0, 1) }}</span>
                <div>
                  <span class="wecom-label">{{ t('myWecom.bot') }}</span>
                  <h3 data-test="bot-name">
                    {{ botLabel }}
                  </h3>
                </div>
              </div>
              <el-tag
                :type="binding.enabled ? 'success' : 'info'"
                data-test="binding-status"
              >
                {{ t(binding.enabled ? 'myWecom.status.enabled' : 'myWecom.status.paused') }}
              </el-tag>
            </div>
            <el-alert
              v-if="awaitingConfirm"
              class="wecom-binding-alert"
              type="warning"
              show-icon
              :closable="false"
              :title="t('myWecom.confirmAuthTitle')"
              :description="t('myWecom.confirmAuthHint', { path: renewPath })"
              data-test="confirm-auth"
            />
            <el-alert
              v-else-if="needsRenew"
              class="wecom-binding-alert"
              type="warning"
              show-icon
              :closable="false"
              :title="t('myWecom.renewNeeded', { path: renewPath })"
              data-test="renew-needed"
            />
            <dl class="wecom-meta">
              <dt>{{ t('myWecom.authorizer') }}</dt>
              <dd data-test="authorizer">
                {{ binding.authorizer_name || '—' }}
              </dd>
              <dt>{{ t('myWecom.boundAt') }}</dt>
              <dd>{{ formatDateTime(binding.bound_at) }}</dd>
              <dt>{{ t('myWecom.verifiedAt') }}</dt>
              <dd>{{ formatDateTime(binding.verified_at) }}</dd>
              <dt>{{ t('myWecom.nextExpiry') }}</dt>
              <dd data-test="next-expiry">
                {{ formatDateTime(binding.next_expiry) }}
              </dd>
            </dl>
            <section class="wecom-setting wecom-setting-row">
              <div>
                <div class="wecom-setting-title">
                  {{ t('myWecom.enableLabel') }}
                </div>
                <p>{{ binding.enabled ? t('myWecom.enabledHint') : t('myWecom.pausedHint', { command: CONNECT }) }}</p>
              </div>
              <el-switch
                :model-value="binding.enabled"
                :loading="busy === 'enabled'"
                :disabled="!!busy && busy !== 'enabled'"
                :aria-label="t('myWecom.enableLabel')"
                data-test="enabled-switch"
                @change="(value: unknown) => setEnabled(value === true)"
              />
            </section>
            <section class="wecom-setting">
              <div class="wecom-setting-title">
                {{ t('myWecom.level') }}
              </div>
              <el-radio-group
                :model-value="binding.authorization_level"
                :disabled="!!busy"
                class="wecom-levels"
                @change="(value: unknown) => setLevel(value as WecomAuthorizationLevel)"
              >
                <el-radio
                  v-for="level in LEVELS"
                  :key="level"
                  :value="level"
                  :data-test="'level-' + level"
                >
                  <span class="wecom-level-name">{{ t('myWecom.levels.' + level) }}</span>
                  <span class="wecom-level-hint">{{ t('myWecom.capabilityHints.' + level) }}</span>
                </el-radio>
              </el-radio-group>
            </section>
            <section class="wecom-setting">
              <div class="wecom-setting-title">
                {{ t('myWecom.capabilitiesTitle') }}
              </div>
              <p>{{ t('myWecom.capabilitiesHint', { days }) }}</p>
              <div
                class="wecom-caps"
                role="table"
              >
                <div
                  class="wecom-caps-row wecom-caps-head"
                  role="row"
                >
                  <span role="columnheader">{{ t('myWecom.service') }}</span>
                  <span
                    v-for="kind in KINDS"
                    :key="kind"
                    role="columnheader"
                  >{{ t('myWecom.kinds.' + kind) }}</span>
                </div>
                <div
                  v-for="cap in binding.capabilities"
                  :key="cap.service"
                  class="wecom-caps-row"
                  role="row"
                  :data-test="'cap-' + cap.service"
                >
                  <span
                    class="wecom-cap-service"
                    role="rowheader"
                  >
                    {{ t('myWecom.services.' + cap.service) }}
                    <a
                      v-if="renewUrl(cap)"
                      :href="renewUrl(cap) ?? undefined"
                      target="_blank"
                      rel="noopener noreferrer"
                      :title="t('myWecom.renewLinkNote')"
                      :data-test="'renew-' + cap.service"
                    >{{ t('myWecom.renewLink') }}</a>
                  </span>
                  <span
                    v-for="kind in KINDS"
                    :key="kind"
                    class="wecom-cap-cell"
                    role="cell"
                    :data-test="'cap-' + cap.service + '-' + kind"
                  >
                    <el-tag
                      size="small"
                      :type="STATE_TAGS[stateOf(cap[kind])]"
                      disable-transitions
                    >{{ t('myWecom.states.' + stateOf(cap[kind])) }}</el-tag>
                    <small v-if="cap[kind]?.state === 'ok' && cap[kind]?.expires_at">{{ t('myWecom.expiresShort', { time: shortTime(cap[kind]?.expires_at ?? '') }) }}</small>
                  </span>
                </div>
              </div>
              <p
                v-if="hasRenewUrl"
                class="wecom-renew-note"
              >
                {{ t('myWecom.renewLinkNote') }}
              </p>
            </section>
            <footer class="wecom-binding-footer">
              <el-button
                :loading="busy === 'check'"
                :disabled="!!busy"
                data-test="check"
                @click="check"
              >
                {{ t('myWecom.check') }}
              </el-button>
              <el-button
                :loading="busy === 'renewed'"
                :disabled="!!busy"
                data-test="renewed"
                @click="renewed"
              >
                {{ t('myWecom.renewed') }}
              </el-button>
              <el-button
                :disabled="!!busy"
                data-test="rebind"
                @click="rebind"
              >
                {{ t('myWecom.rebind') }}
              </el-button>
              <el-button
                type="danger"
                plain
                :loading="busy === 'unbind'"
                :disabled="!!busy"
                data-test="unbind"
                @click="unbind"
              >
                {{ t('myWecom.unbind') }}
              </el-button>
            </footer>
          </article>
          <article
            v-else
            class="wecom-unbound"
            data-test="unbound"
          >
            <el-alert
              v-if="binding.error && te('myWecom.errors.' + binding.error)"
              type="warning"
              show-icon
              :closable="false"
              :title="t('myWecom.errors.' + binding.error)"
              data-test="binding-error"
            />
            <h3>{{ t('myWecom.unbound.title') }}</h3>
            <p>{{ t('myWecom.unbound.hint') }}</p>
            <el-button
              type="primary"
              size="large"
              :disabled="!binding.identity_linked || !!busy"
              data-test="scan"
              @click="scanning = true"
            >
              {{ t('myWecom.scanBind') }}
            </el-button>
            <p
              v-if="!binding.identity_linked"
              class="wecom-unlinked"
              data-test="identity-unlinked"
            >
              {{ t('myWecom.unbound.identityUnlinked') }}
            </p>
          </article>
        </template>
      </div>
      <aside class="wecom-connect-guide">
        <div class="wecom-connect-title">
          {{ t('myWecom.guide.title') }}
        </div>
        <p>{{ t('myWecom.guide.model') }}</p>
        <p>{{ t('myWecom.guide.scope') }}</p>
        <p>{{ t('myWecom.guide.connect', { command: CONNECT }) }}</p>
        <p>{{ t('myWecom.guide.disconnect', { command: DISCONNECT, connect: CONNECT }) }}</p>
        <div class="wecom-tiers-title">
          {{ t('myWecom.guide.tiersTitle') }}
        </div>
        <dl class="wecom-tiers">
          <template
            v-for="level in LEVELS"
            :key="level"
          >
            <dt>{{ t('myWecom.levels.' + level) }}</dt>
            <dd>{{ t('myWecom.capabilityHints.' + level) }}</dd>
          </template>
        </dl>
        <div class="wecom-tiers-title">
          {{ t('myWecom.guide.renewTitle', { days }) }}
        </div>
        <p>{{ t('myWecom.guide.renew', { days, path: renewPath }) }}</p>
        <p>{{ t('myWecom.guide.renewDone') }}</p>
        <span
          v-if="binding?.retention_notice"
          class="wecom-private-label"
          data-test="retention-notice"
        >{{ binding.retention_notice }}</span>
      </aside>
    </div>
    <WecomBindDialog
      v-model:visible="scanning"
      @changed="apply"
      @bound="onBound"
    />
  </section>
</template>
<style scoped>
.wecom-page-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
.wecom-page-header h2 { margin: 0 0 8px; font-size: 26px; letter-spacing: -.5px; }
.wecom-page-header p { margin: 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.wecom-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 380px); gap: 28px; align-items: start; }
.wecom-main { min-width: 0; }
.wecom-connect-guide { padding: 18px 22px; border-left: 3px solid var(--el-color-primary); background: var(--el-fill-color-light); border-radius: 0 8px 8px 0; }
.wecom-connect-title { font-weight: 600; font-size: 15px; }
.wecom-connect-guide p { margin: 7px 0; line-height: 1.7; }
.wecom-tiers-title { margin-top: 14px; font-weight: 600; font-size: 14px; }
.wecom-tiers { display: grid; grid-template-columns: 1fr; gap: 2px; margin: 8px 0 12px; line-height: 1.7; }
.wecom-tiers dt { font-weight: 600; }
.wecom-tiers dd { margin: 0 0 6px; color: var(--el-text-color-regular); }
.wecom-private-label { display: block; margin-top: 12px; font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.7; }
.wecom-binding, .wecom-unbound { border: 1px solid var(--el-border-color-light); border-radius: 12px; background: var(--el-bg-color); min-width: 0; overflow: hidden; }
.is-enabled { border-color: var(--el-color-primary-light-7); }
.wecom-binding-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 22px 24px; }
.wecom-bot-identity { display: flex; align-items: center; gap: 12px; min-width: 0; }
.wecom-bot-avatar { display: grid; place-items: center; width: 40px; height: 40px; flex-shrink: 0; border-radius: 10px; color: var(--el-color-primary); background: var(--el-color-primary-light-9); font-weight: 600; font-size: 20px; }
.wecom-label { font-size: 12px; color: var(--el-text-color-secondary); }
h3 { margin: 0; font-size: 17px; overflow-wrap: anywhere; }
.wecom-binding-alert { margin: 0 24px 16px; width: auto; }
.wecom-meta { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 8px 14px; margin: 0 24px 16px; font-size: 13px; line-height: 1.7; }
.wecom-meta dt { color: var(--el-text-color-secondary); }
.wecom-meta dd { margin: 0; overflow-wrap: anywhere; }
.wecom-setting { padding: 16px 24px; border-top: 1px solid var(--el-border-color-lighter); }
.wecom-setting-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.wecom-setting-title { font-weight: 600; font-size: 14px; }
.wecom-setting p { margin: 6px 0 0; font-size: 13px; line-height: 1.7; color: var(--el-text-color-secondary); }
.wecom-levels { display: flex; flex-direction: column; align-items: stretch; gap: 10px; margin-top: 10px; }
.wecom-levels .el-radio { height: auto; margin-right: 0; align-items: flex-start; white-space: normal; }
.wecom-levels :deep(.el-radio__label) { white-space: normal; }
.wecom-levels :deep(.el-radio__input) { margin-top: 3px; }
.wecom-level-name { display: block; font-weight: 600; line-height: 1.6; }
.wecom-level-hint { display: block; font-weight: normal; font-size: 12px; line-height: 1.6; color: var(--el-text-color-secondary); }
.wecom-caps { margin-top: 12px; font-size: 13px; }
.wecom-caps-row { display: grid; grid-template-columns: minmax(0, 1.2fr) repeat(3, minmax(0, 1fr)); gap: 8px; align-items: start; padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.wecom-caps-head { padding-top: 0; color: var(--el-text-color-secondary); font-size: 12px; }
.wecom-cap-service { display: flex; flex-direction: column; gap: 2px; overflow-wrap: anywhere; }
.wecom-cap-service a { font-size: 12px; color: var(--el-color-primary); }
.wecom-cap-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; min-width: 0; }
.wecom-cap-cell .el-tag { max-width: 100%; height: auto; min-height: 20px; white-space: normal; line-height: 1.4; }
.wecom-cap-cell small { font-size: 11px; color: var(--el-text-color-secondary); line-height: 1.5; }
.wecom-setting .wecom-renew-note { font-size: 12px; }
.wecom-binding-footer { display: flex; flex-wrap: wrap; gap: 8px 12px; padding: 16px 24px; border-top: 1px solid var(--el-border-color-lighter); }
.wecom-binding-footer .el-button + .el-button { margin-left: 0; }
.wecom-unbound { display: flex; flex-direction: column; align-items: flex-start; gap: 12px; padding: 24px; }
.wecom-unbound p { margin: 0; line-height: 1.7; color: var(--el-text-color-secondary); }
.wecom-unbound .wecom-unlinked { font-size: 13px; color: var(--el-color-warning-dark-2); }
@media (max-width: 1100px) {
  .wecom-layout { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 600px) {
  .wecom-page-header { align-items: flex-start; }
  .wecom-binding-heading, .wecom-binding-footer, .wecom-setting, .wecom-unbound { padding: 16px; }
  .wecom-binding-alert, .wecom-meta { margin-left: 16px; margin-right: 16px; }
  .wecom-meta { grid-template-columns: 1fr; gap: 2px; }
  .wecom-meta dd { margin-bottom: 8px; }
  .wecom-caps-row { grid-template-columns: minmax(0, 1fr) repeat(3, minmax(0, 1fr)); gap: 4px; }
}
</style>
