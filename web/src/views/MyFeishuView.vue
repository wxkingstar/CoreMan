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
    <header class="cm-page-header">
      <h2>{{ t('menu.myFeishu') }}</h2>
      <p class="cm-page-intro">
        {{ t('myFeishu.intro') }}
      </p>
    </header>
    <el-alert
      :title="t('myFeishu.connectHint', { command: '连接我的飞书' })"
      type="info"
      :closable="false"
      show-icon
    >
      <p>{{ t('myFeishu.privateOnly') }}</p>
      <p>{{ t('myFeishu.requestHint', { command: '查看最近的聊天' }) }}</p>
    </el-alert>
    <div class="feishu-toolbar">
      <el-button
        :loading="loading"
        :disabled="!!revoking"
        @click="load()"
      >
        {{ t('myFeishu.refresh') }}
      </el-button>
    </div>
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
        >
          <div class="feishu-grant-heading">
            <h3>{{ row.bot_name }}</h3>
            <el-tag :type="row.status === 'connected' ? 'success' : row.status === 'revoked' ? 'info' : 'warning'">
              {{ t('myFeishu.status.' + row.status) }}
            </el-tag>
          </div>
          <p v-if="row.status === 'pending'">
            {{ t('myFeishu.pendingHint') }}
          </p>
          <p v-if="row.status === 'expired' || row.status === 'revoked'">
            {{ t('myFeishu.reconnectHint', { command: '连接我的飞书' }) }}
          </p>
          <dl>
            <dt>{{ t('myFeishu.level') }}</dt>
            <dd>{{ row.status === 'selecting' ? t('myFeishu.unselected') : t('myFeishu.levels.' + (row.authorization_level || 'legacy_readonly')) }}</dd>
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
            <dt>{{ t(row.status === 'pending' ? 'myFeishu.pendingExpiresAt' : 'myFeishu.expiresAt') }}</dt>
            <dd>{{ row.expires_at ? formatDateTime(row.expires_at) : '—' }}</dd>
          </dl>
          <el-button
            v-if="row.status !== 'revoked'"
            :data-test="'revoke-' + row.bot_id"
            type="danger"
            plain
            :disabled="!!revoking"
            :loading="revoking === row.bot_id"
            @click="revoke(row)"
          >
            {{ t('myFeishu.revoke') }}
          </el-button>
        </article>
      </div>
    </template>
  </section>
</template>
<style scoped>
.feishu-toolbar { margin: 16px 0; }
.feishu-grants { display: grid; gap: 16px; }
.feishu-grant { padding: 20px; border: 1px solid var(--el-border-color); border-radius: 12px; background: var(--el-bg-color); min-width: 0; }
.feishu-grant-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
h3 { margin: 0; overflow-wrap: anywhere; }
dl { display: grid; grid-template-columns: minmax(80px, 120px) minmax(0, 1fr); gap: 12px; }
dt { color: var(--el-text-color-secondary); }
dd { margin: 0; overflow-wrap: anywhere; }
.feishu-scope, .feishu-requested-scope, .feishu-missing-scope { display: block; }
.feishu-scope-hint { margin: 0 0 8px; color: var(--el-text-color-secondary); }
summary { cursor: pointer; }
</style>
