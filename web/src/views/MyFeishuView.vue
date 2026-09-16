<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import LoadState from '@/components/LoadState.vue'
import { feishuAuthorizations, type FeishuAuthorization } from '@/api/feishuAuthorizations'
import { formatDateTime } from '@/utils/format'
const { t } = useI18n()
const rows = ref<FeishuAuthorization[]>([])
const loading = ref(false), error = ref(''), revoking = ref('')
async function load() {
  if (loading.value) return
  loading.value = true; error.value = ''
  try { rows.value = (await feishuAuthorizations.list()).items }
  catch { error.value = t('myFeishu.loadError') }
  finally { loading.value = false }
}
async function revoke(row: FeishuAuthorization) {
  if (revoking.value) return
  revoking.value = row.bot_id
  try {
    await ElMessageBox.confirm(t('myFeishu.confirmRevoke', { name: row.bot_name }), t('myFeishu.revoke'), { type: 'warning' })
    await feishuAuthorizations.revoke(row.bot_id)
    ElMessage.success(t('myFeishu.revoked'))
    await load()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(t('myFeishu.revokeError')) }
  finally { revoking.value = '' }
}
onMounted(load)
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
        @click="load"
      >
        {{ t('myFeishu.refresh') }}
      </el-button>
    </div>
    <LoadState
      :loading="loading"
      :error="error"
      @retry="load"
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
            <dt>{{ t('myFeishu.scopes') }}</dt>
            <dd>
              <span
                v-for="scope in row.scopes"
                :key="scope"
                class="feishu-scope"
              >{{ scope }}</span><span v-if="!row.scopes.length">—</span>
            </dd>
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
.feishu-scope { display: block; }
</style>
