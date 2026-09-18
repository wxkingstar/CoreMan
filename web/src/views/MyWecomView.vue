<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import LoadState from '@/components/LoadState.vue'
import { wecomAuthorizations, type WecomAuthorization } from '@/api/wecomAuthorizations'
import { formatDateTime } from '@/utils/format'
// 私聊里要原样发送的命令，不随界面语言翻译。
const CONNECT = '连接企业微信', DISCONNECT = '断开企业微信'
const LEVELS = ['readonly', 'all_except_send', 'all'] as const
const { t } = useI18n()
const rows = ref<WecomAuthorization[]>([])
const loading = ref(false), error = ref(''), revoking = ref('')
let requestVersion = 0
let refreshing = false
let timer: ReturnType<typeof setInterval> | undefined
async function load(background = false) {
  if (background && (refreshing || loading.value || revoking.value)) return
  const version = ++requestVersion
  if (background) refreshing = true
  else { loading.value = true; error.value = '' }
  try {
    const result = await wecomAuthorizations.list()
    if (version === requestVersion) { rows.value = result.items; error.value = '' }
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
function tagType(status: WecomAuthorization['status']) {
  return status === 'connected' ? 'success' : status === 'revoked' ? 'info' : 'warning'
}
async function revoke(row: WecomAuthorization) {
  if (revoking.value) return
  revoking.value = row.bot_id
  try {
    await ElMessageBox.confirm(t('myWecom.confirmRevoke', { name: row.bot_name }), t('myWecom.revoke'), { type: 'warning' })
    await wecomAuthorizations.revoke(row.bot_id)
    ElMessage.success(t('myWecom.revoked'))
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(t('myWecom.revokeError')) }
  finally { revoking.value = '' }
}
onMounted(() => {
  void load()
  window.addEventListener('focus', refreshVisible)
  document.addEventListener('visibilitychange', refreshVisible)
  timer = setInterval(refreshVisible, 15000)
})
onUnmounted(() => {
  ++requestVersion
  clearInterval(timer)
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
        :disabled="!!revoking"
        @click="load()"
      >
        {{ t('myWecom.refresh') }}
      </el-button>
    </header>
    <aside class="wecom-connect-guide">
      <div class="wecom-connect-title">
        {{ t('myWecom.connectTitle') }}
      </div>
      <p>{{ t('myWecom.authorizerHint') }}</p>
      <p>{{ t('myWecom.connectHint', { command: CONNECT }) }}</p>
      <p>{{ t('myWecom.disconnectHint', { command: DISCONNECT }) }}</p>
      <div class="wecom-tiers-title">
        {{ t('myWecom.tiersTitle') }}
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
      <p>{{ t('myWecom.retentionNotice') }}</p>
      <span class="wecom-private-label">{{ t('myWecom.privateOnly') }}</span>
    </aside>
    <LoadState
      :loading="loading"
      :error="error"
      @retry="load()"
    />
    <template v-if="!loading && !error">
      <el-empty
        v-if="!rows.length"
        :description="t('myWecom.empty')"
      />
      <div
        v-else
        class="wecom-grants"
      >
        <article
          v-for="row in rows"
          :key="row.bot_id"
          class="wecom-grant"
          :class="{ 'is-connected': row.status === 'connected' }"
        >
          <div class="wecom-grant-heading">
            <div class="wecom-bot-identity">
              <span
                class="wecom-bot-avatar"
                aria-hidden="true"
              >{{ row.bot_name.slice(0, 1) }}</span>
              <h3>{{ row.bot_name }}</h3>
            </div>
            <el-tag :type="tagType(row.status)">
              {{ t('myWecom.status.' + row.status) }}
            </el-tag>
          </div>
          <p v-if="row.status === 'selecting'">
            {{ t('myWecom.selectingHint') }}
          </p>
          <p v-else-if="row.status === 'expired'">
            {{ t('myWecom.expiredHint', { command: CONNECT }) }}
          </p>
          <p v-else-if="row.status === 'revoked'">
            {{ t('myWecom.reconnectHint', { command: CONNECT }) }}
          </p>
          <div class="wecom-permission-overview">
            <span class="wecom-permission-label">{{ t(row.status === 'connected' ? 'myWecom.effectivePermissions' : 'myWecom.level') }}</span>
            <h4 v-if="row.status === 'connected'">
              {{ t('myWecom.levels.' + row.authorization_level) }}
            </h4>
            <h4 v-else>
              {{ t(row.status === 'revoked' ? 'myWecom.notConnected' : 'myWecom.unselected') }}
            </h4>
            <p>{{ row.status === 'connected' ? t('myWecom.capabilityHints.' + row.authorization_level) : t('myWecom.inactive') }}</p>
          </div>
          <p
            v-if="row.status === 'connected'"
            class="wecom-usage-hint"
          >
            {{ t('myWecom.naturalQueryHint') }}
          </p>
          <dl
            v-if="row.verified_at || (row.status === 'selecting' && row.selection_expires_at)"
            class="wecom-meta"
          >
            <template v-if="row.verified_at">
              <dt>{{ t('myWecom.verifiedAt') }}</dt>
              <dd data-test="verified-at">
                {{ formatDateTime(row.verified_at) }}
              </dd>
            </template>
            <template v-if="row.status === 'selecting' && row.selection_expires_at">
              <dt>{{ t('myWecom.selectionExpiresAt') }}</dt>
              <dd>{{ formatDateTime(row.selection_expires_at) }}</dd>
            </template>
          </dl>
          <footer class="wecom-grant-footer">
            <span>{{ t('myWecom.supportedTools') }}</span>
            <el-button
              v-if="row.status !== 'revoked'"
              :data-test="'revoke-' + row.bot_id"
              type="danger"
              link
              :disabled="!!revoking"
              :loading="revoking === row.bot_id"
              @click="revoke(row)"
            >
              {{ t('myWecom.revoke') }}
            </el-button>
          </footer>
        </article>
      </div>
    </template>
  </section>
</template>
<style scoped>
.wecom-page-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
.wecom-page-header h2 { margin: 0 0 8px; font-size: 26px; letter-spacing: -.5px; }
.wecom-page-header p { margin: 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.wecom-connect-guide { padding: 18px 22px; border-left: 3px solid var(--el-color-primary); background: var(--el-fill-color-light); margin-bottom: 28px; border-radius: 0 8px 8px 0; }
.wecom-connect-title { font-weight: 600; font-size: 15px; }
.wecom-connect-guide p { margin: 7px 0; line-height: 1.7; }
.wecom-tiers-title { margin-top: 14px; font-weight: 600; font-size: 14px; }
.wecom-tiers { display: grid; grid-template-columns: minmax(0, 16em) minmax(0, 1fr); gap: 6px 16px; margin: 8px 0 12px; line-height: 1.7; }
.wecom-tiers dt { font-weight: 600; }
.wecom-tiers dd { margin: 0; color: var(--el-text-color-regular); }
.wecom-private-label { font-size: 12px; color: var(--el-text-color-secondary); }
.wecom-grants { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr)); gap: 24px; align-items: start; }
.wecom-grant { border: 1px solid var(--el-border-color-light); border-radius: 12px; background: var(--el-bg-color); min-width: 0; overflow: hidden; }
.wecom-grant-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 22px 24px; }
.wecom-bot-identity { display: flex; align-items: center; gap: 12px; min-width: 0; }
.wecom-bot-avatar { display: grid; place-items: center; width: 40px; height: 40px; flex-shrink: 0; border-radius: 10px; color: var(--el-color-primary); background: var(--el-color-primary-light-9); font-weight: 600; font-size: 20px; }
h3 { margin: 0; font-size: 17px; overflow-wrap: anywhere; }
.wecom-grant > p { margin: 0 24px 18px; line-height: 1.7; color: var(--el-text-color-secondary); font-size: 13px; }
.wecom-permission-overview { margin: 0 24px 16px; padding: 20px; border-radius: 8px; background: var(--el-fill-color-light); }
.is-connected .wecom-permission-overview { background: var(--el-color-primary-light-9); }
.wecom-permission-label { font-size: 12px; color: var(--el-text-color-secondary); }
h4 { margin: 8px 0 12px; font-size: 21px; line-height: 1.45; letter-spacing: -.3px; }
.is-connected h4 { color: var(--el-color-primary); }
.wecom-permission-overview p { margin: 0; line-height: 1.8; font-size: 14px; }
.wecom-meta { display: grid; grid-template-columns: 180px minmax(0, 1fr); gap: 8px 14px; margin: 0 24px 16px; font-size: 13px; line-height: 1.7; }
.wecom-meta dt { color: var(--el-text-color-secondary); }
.wecom-meta dd { margin: 0; overflow-wrap: anywhere; }
.wecom-grant-footer { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 16px 24px; border-top: 1px solid var(--el-border-color-lighter); }
.wecom-grant-footer > span { max-width: 52ch; font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
@media (max-width: 600px) {
  .wecom-grant-heading, .wecom-grant-footer { padding: 16px; }
  .wecom-permission-overview { margin: 0 16px 16px; padding: 16px; }
  .wecom-grant > p, .wecom-meta { margin-left: 16px; margin-right: 16px; }
  .wecom-tiers, .wecom-meta { grid-template-columns: 1fr; gap: 2px; }
  .wecom-tiers dd, .wecom-meta dd { margin-bottom: 8px; }
  h4 { font-size: 19px; }
}
</style>
