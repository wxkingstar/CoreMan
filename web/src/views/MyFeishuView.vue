<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import LoadState from '@/components/LoadState.vue'
import { feishuAuthorizations, type FeishuAuthorization } from '@/api/feishuAuthorizations'
import { formatDateTime } from '@/utils/format'
const { t } = useI18n()
const rows = ref<FeishuAuthorization[]>([])
const expandedScopes = ref<Record<string, boolean>>({})
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
    const result = await feishuAuthorizations.list()
    if (version === requestVersion) { rows.value = result.items; error.value = '' }
  } catch {
    if (version === requestVersion && !background) error.value = t('myFeishu.loadError')
  } finally {
    if (background) refreshing = false
    if (version === requestVersion) loading.value = false
  }
}
function refreshVisible() {
  if (document.visibilityState !== 'hidden') void load(true)
}
async function revoke(row: FeishuAuthorization) {
  if (revoking.value) return
  revoking.value = row.bot_id
  try {
    await ElMessageBox.confirm(t('myFeishu.confirmRevoke', { name: row.bot_name }), t('myFeishu.revoke'), { type: 'warning' })
    const result = await feishuAuthorizations.revoke(row.bot_id)
    if (result.remote_revoked) ElMessage.success(t('myFeishu.revoked'))
    else ElMessage.warning(t('myFeishu.localRevoked'))
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(t('myFeishu.revokeError')) }
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
    <header class="feishu-page-header">
      <div>
        <h2>{{ t('menu.myFeishu') }}</h2>
        <p>{{ t('myFeishu.intro') }}</p>
      </div>
      <el-button
        :loading="loading"
        :disabled="!!revoking"
        @click="load()"
      >
        {{ t('myFeishu.refresh') }}
      </el-button>
    </header>
    <aside class="feishu-connect-guide">
      <div class="feishu-connect-title">
        {{ t('myFeishu.connectTitle') }}
      </div>
      <p>{{ t('myFeishu.connectHint', { command: '连接飞书' }) }}</p>
      <p>{{ t('myFeishu.retentionNotice') }}</p>
      <p>{{ t('myFeishu.modeHint') }}</p>
      <span class="feishu-private-label">{{ t('myFeishu.privateOnly') }}</span>
    </aside>
    <LoadState
      :loading="loading"
      :error="error"
      @retry="load()"
    />
    <template v-if="!loading && !error">
      <el-empty
        v-if="!rows.length"
        :description="t('myFeishu.empty')"
      />
      <div
        v-else
        class="feishu-grants"
      >
        <article
          v-for="row in rows"
          :key="row.bot_id"
          class="feishu-grant"
          :class="{ 'is-connected': row.status === 'connected' }"
        >
          <div class="feishu-grant-heading">
            <div class="feishu-bot-identity">
              <span
                class="feishu-bot-avatar"
                aria-hidden="true"
              >{{ row.bot_name.slice(0, 1) }}</span>
              <h3>{{ row.bot_name }}</h3>
            </div>
            <el-tag :type="row.status === 'connected' ? 'success' : row.status === 'revoked' ? 'info' : 'warning'">
              {{ t('myFeishu.status.' + row.status) }}
            </el-tag>
          </div>
          <p v-if="row.status === 'pending'">
            {{ t('myFeishu.pendingHint') }}
          </p>
          <p v-if="row.status === 'expired' || row.status === 'revoked'">
            {{ t('myFeishu.reconnectHint', { command: '连接飞书' }) }}
          </p>
          <p v-if="row.status === 'connected' && row.access_token_expired">
            {{ t(row.refresh_available ? 'myFeishu.tokenRefreshAvailable' : 'myFeishu.tokenExpired') }}
          </p>
          <div class="feishu-permission-overview">
            <span class="feishu-permission-label">{{ t(row.status === 'connected' ? 'myFeishu.effectivePermissions' : 'myFeishu.level') }}</span>
            <h4>{{ row.status === 'selecting' ? t('myFeishu.unselected') : t('myFeishu.levels.' + (row.authorization_level || 'legacy_readonly')) }}</h4>
            <p>{{ row.status !== 'connected' ? t('myFeishu.inactive') : t('myFeishu.capabilityHints.' + (row.authorization_level || 'legacy_readonly')) }}</p>
            <p
              v-if="row.status === 'connected' && row.missing_scopes?.length"
              class="feishu-missing-warning"
            >
              {{ t('myFeishu.incomplete') }}
            </p>
          </div>
          <p
            v-if="row.status === 'connected'"
            class="feishu-usage-hint"
          >
            {{ t('myFeishu.naturalQueryHint') }}
          </p>
          <details class="feishu-advanced">
            <summary>{{ t('myFeishu.advancedDetails') }}</summary>
            <p class="feishu-scope-hint">
              {{ t('myFeishu.credentialHint') }}
            </p>
            <dl>
              <dt>{{ t(row.status === 'pending' ? 'myFeishu.pendingExpiresAt' : 'myFeishu.expiresAt') }}</dt>
              <dd>{{ row.expires_at ? formatDateTime(row.expires_at) : '—' }}</dd>
              <dt>{{ t('myFeishu.allowedScopes') }}</dt>
              <dd>
                <p class="feishu-scope-hint">
                  {{ t('myFeishu.scopeHint') }}
                </p>
                <details v-if="row.requested_scopes?.length">
                  <summary>{{ t('myFeishu.scopeCount', { count: row.requested_scopes.length }) }}</summary>
                  <span
                    v-for="scope in row.requested_scopes"
                    :key="scope"
                    class="feishu-requested-scope"
                  >{{ scope }}</span>
                </details>
                <span v-else>—</span>
              </dd>
              <dt>{{ t('myFeishu.scopes') }}</dt>
              <dd>
                <span
                  v-for="scope in (expandedScopes[row.bot_id] ? row.scopes : row.scopes.slice(0, 5))"
                  :key="scope"
                  class="feishu-scope"
                >{{ scope }}</span><span v-if="!row.scopes.length">—</span>
                <el-button
                  v-if="row.scopes.length > 5"
                  link
                  type="primary"
                  :data-test="'toggle-scopes-' + row.bot_id"
                  :aria-expanded="!!expandedScopes[row.bot_id]"
                  @click="expandedScopes[row.bot_id] = !expandedScopes[row.bot_id]"
                >
                  {{ expandedScopes[row.bot_id] ? t('myFeishu.collapseScopes') : t('myFeishu.expandScopes', { count: row.scopes.length }) }}
                </el-button>
              </dd>
              <template v-if="row.missing_scopes?.length">
                <dt>{{ t('myFeishu.missingScopes') }}</dt>
                <dd>
                  <details>
                    <summary>{{ t('myFeishu.scopeCount', { count: row.missing_scopes.length }) }}</summary>
                    <span
                      v-for="scope in row.missing_scopes"
                      :key="scope"
                      class="feishu-missing-scope"
                    >{{ scope }}</span>
                  </details>
                </dd>
              </template>
            </dl>
          </details>
          <footer class="feishu-grant-footer">
            <span>{{ t('myFeishu.supportedTools') }}</span>
            <el-button
              v-if="row.status !== 'revoked'"
              :data-test="'revoke-' + row.bot_id"
              type="danger"
              link
              :disabled="!!revoking"
              :loading="revoking === row.bot_id"
              @click="revoke(row)"
            >
              {{ t('myFeishu.revoke') }}
            </el-button>
          </footer>
        </article>
      </div>
    </template>
  </section>
</template>
<style scoped>
.feishu-page-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
.feishu-page-header h2 { margin: 0 0 8px; font-size: 26px; letter-spacing: -.5px; }
.feishu-page-header p { margin: 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.feishu-connect-guide { padding: 18px 22px; border-left: 3px solid var(--el-color-primary); background: var(--el-fill-color-light); margin-bottom: 28px; border-radius: 0 8px 8px 0; }
.feishu-connect-title { font-weight: 600; font-size: 15px; }
.feishu-connect-guide p { margin: 7px 0; line-height: 1.7; }
.feishu-private-label { font-size: 12px; color: var(--el-text-color-secondary); }
.feishu-grants { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr)); gap: 24px; align-items: start; }
.feishu-grant { border: 1px solid var(--el-border-color-light); border-radius: 12px; background: var(--el-bg-color); min-width: 0; overflow: hidden; }
.feishu-grant-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 22px 24px; }
.feishu-bot-identity { display: flex; align-items: center; gap: 12px; min-width: 0; }
.feishu-bot-avatar { display: grid; place-items: center; width: 40px; height: 40px; flex-shrink: 0; border-radius: 10px; color: var(--el-color-primary); background: var(--el-color-primary-light-9); font-weight: 600; font-size: 20px; }
h3 { margin: 0; font-size: 17px; overflow-wrap: anywhere; }
.feishu-grant > p { margin: 0 24px 18px; line-height: 1.7; color: var(--el-text-color-secondary); font-size: 13px; }
.feishu-permission-overview { margin: 0 24px 16px; padding: 20px; border-radius: 8px; background: var(--el-fill-color-light); }
.is-connected .feishu-permission-overview { background: var(--el-color-primary-light-9); }
.feishu-permission-label { font-size: 12px; color: var(--el-text-color-secondary); }
h4 { margin: 8px 0 12px; font-size: 21px; line-height: 1.45; letter-spacing: -.3px; }
.is-connected h4 { color: var(--el-color-primary); }
.feishu-permission-overview p { margin: 0; line-height: 1.8; font-size: 14px; }
.feishu-permission-overview .feishu-missing-warning { margin-top: 12px; color: var(--el-color-warning-dark-2); }
.feishu-advanced { margin: 0 24px; border-top: 1px solid var(--el-border-color-lighter); padding: 16px 0; }
.feishu-advanced > summary { color: var(--el-text-color-secondary); font-size: 13px; }
.feishu-advanced[open] > summary { margin-bottom: 16px; }
dl { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 14px; font-size: 13px; line-height: 1.7; }
dt { color: var(--el-text-color-secondary); }
dd { margin: 0; overflow-wrap: anywhere; }
.feishu-scope, .feishu-requested-scope, .feishu-missing-scope { display: block; font-size: 12px; }
.feishu-scope-hint { margin: 0 0 8px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.8; }
.feishu-grant-footer { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 16px 24px; border-top: 1px solid var(--el-border-color-lighter); }
.feishu-grant-footer > span { max-width: 52ch; font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
summary { cursor: pointer; }
summary:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 4px; }
@media (max-width: 600px) {
  .feishu-grant-heading, .feishu-grant-footer { padding: 16px; }
  .feishu-permission-overview { margin: 0 16px 16px; padding: 16px; }
  .feishu-advanced { margin: 0 16px; }
  .feishu-grant > p { margin-left: 16px; margin-right: 16px; }
  dl { grid-template-columns: 1fr; gap: 6px; } dd { margin-bottom: 8px; }
  h4 { font-size: 19px; }
}
</style>
