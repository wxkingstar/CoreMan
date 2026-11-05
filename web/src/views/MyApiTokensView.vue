<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiTokens, type ApiToken, type ApiTokenBot } from '@/api/apiAccess'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const rows = ref<ApiToken[]>([])
const options = ref<ApiTokenBot[]>([])
const error = ref('')
const busy = ref(false)
const creating = ref(false)
const plaintext = ref('')
const form = reactive({ name: '', botIds: [] as string[], days: 90 })

async function load() {
  busy.value = true
  try { rows.value = await apiTokens.list(); error.value = '' }
  catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
async function openCreate() {
  Object.assign(form, { name: '', botIds: [], days: 90 })
  try { options.value = await apiTokens.bots() }
  catch (e) { ElMessage.error(errorMessage(e)); return }
  creating.value = true
}
async function create() {
  if (!form.name.trim()) return
  if (!form.botIds.length) { ElMessage.warning(t('apiAccess.tokens.pickBots')); return }
  busy.value = true
  try {
    const created = await apiTokens.create(form.name.trim(), form.botIds, form.days)
    creating.value = false
    plaintext.value = created.token ?? ''
    await load()
  } catch (e) { ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
async function revoke(row: ApiToken) {
  try {
    await ElMessageBox.confirm(t('apiAccess.tokens.revokeConfirm', { name: row.name }), t('apiAccess.tokens.revoke'), {
      type: 'warning', confirmButtonText: t('apiAccess.tokens.confirm'), cancelButtonText: t('apiAccess.tokens.cancel'),
    })
  } catch { return }
  busy.value = true
  try { await apiTokens.revoke(row.id); ElMessage.success(t('apiAccess.tokens.revoked')); await load() }
  catch (e) { ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
async function copy() {
  try { await navigator.clipboard.writeText(plaintext.value); ElMessage.success(t('apiAccess.tokens.copied')) }
  catch { /* 剪贴板不可用：用户可以手动选中复制 */ }
}
onMounted(load)
</script>
<template>
  <section class="api-tokens">
    <h1>{{ t('menu.myApiTokens') }}</h1>
    <p class="intro">
      {{ t('apiAccess.tokens.intro') }}
    </p>
    <div class="actions">
      <el-button
        type="primary"
        data-test="create-token"
        @click="openCreate"
      >
        {{ t('apiAccess.tokens.create') }}
      </el-button>
      <el-button
        :loading="busy"
        @click="load"
      >
        {{ t('apiAccess.tokens.refresh') }}
      </el-button>
    </div>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
    />
    <el-empty
      v-if="!busy && !error && !rows.length"
      :description="t('apiAccess.tokens.empty')"
    />
    <el-table
      v-if="rows.length"
      :data="rows"
      row-key="id"
    >
      <el-table-column
        :label="t('apiAccess.tokens.name')"
        prop="name"
        min-width="120"
      />
      <el-table-column
        :label="t('apiAccess.tokens.prefix')"
        min-width="130"
      >
        <template #default="{ row }">
          <code>{{ row.prefix }}…</code>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('apiAccess.tokens.bots')"
        min-width="180"
      >
        <template #default="{ row }">
          <el-tag
            v-for="bot in row.bots"
            :key="bot.id"
            size="small"
            class="bot-tag"
            :type="bot.api_access ? 'info' : 'warning'"
            :title="bot.api_access ? '' : t('apiAccess.tokens.apiOff')"
          >
            {{ bot.name }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('apiAccess.tokens.status')">
        <template #default="{ row }">
          <el-tag
            size="small"
            :type="row.status === 'active' ? 'success' : 'info'"
          >
            {{ t(`apiAccess.tokens.statuses.${row.status}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('apiAccess.tokens.expiresAt')">
        <template #default="{ row }">
          {{ formatDateTime(row.expires_at) }}
        </template>
      </el-table-column>
      <el-table-column :label="t('apiAccess.tokens.lastUsedAt')">
        <template #default="{ row }">
          {{ row.last_used_at ? formatDateTime(row.last_used_at) : t('apiAccess.tokens.never') }}
        </template>
      </el-table-column>
      <el-table-column width="100">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'active'"
            link
            type="danger"
            :data-test="'revoke-' + row.id"
            @click="revoke(row)"
          >
            {{ t('apiAccess.tokens.revoke') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <section class="usage">
      <h2>{{ t('apiAccess.tokens.usageTitle') }}</h2>
      <p>{{ t('apiAccess.tokens.usage') }}</p>
    </section>

    <el-dialog
      v-model="creating"
      :title="t('apiAccess.tokens.create')"
      :close-on-click-modal="false"
      width="min(560px, 92vw)"
    >
      <el-form label-position="top">
        <el-form-item
          :label="t('apiAccess.tokens.name')"
          required
        >
          <el-input
            v-model="form.name"
            maxlength="64"
            :placeholder="t('apiAccess.tokens.namePlaceholder')"
            data-test="token-name"
          />
        </el-form-item>
        <el-form-item
          :label="t('apiAccess.tokens.bots')"
          required
        >
          <el-select
            v-model="form.botIds"
            multiple
            filterable
            style="width: 100%"
            data-test="token-bots"
            :no-data-text="t('apiAccess.tokens.noBots')"
          >
            <el-option
              v-for="bot in options"
              :key="bot.id"
              :label="bot.api_access ? bot.name : `${bot.name}（${t('apiAccess.tokens.apiOff')}）`"
              :value="bot.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('apiAccess.tokens.days')">
          <el-input-number
            v-model="form.days"
            :min="1"
            :max="365"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="creating = false">
          {{ t('apiAccess.tokens.cancel') }}
        </el-button>
        <el-button
          type="primary"
          :loading="busy"
          :disabled="!form.name.trim()"
          data-test="submit-token"
          @click="create"
        >
          {{ t('apiAccess.tokens.confirm') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      :model-value="!!plaintext"
      :title="t('apiAccess.tokens.created')"
      :close-on-click-modal="false"
      width="min(560px, 92vw)"
      @close="plaintext = ''"
    >
      <el-alert
        :title="t('apiAccess.tokens.createdHint')"
        type="warning"
        :closable="false"
        show-icon
      />
      <code
        class="plaintext"
        data-test="token-plaintext"
      >{{ plaintext }}</code>
      <template #footer>
        <el-button @click="copy">
          {{ t('apiAccess.tokens.copy') }}
        </el-button>
        <el-button
          type="primary"
          @click="plaintext = ''"
        >
          {{ t('apiAccess.tokens.close') }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
<style scoped>
.intro { max-width: 80ch; color: var(--el-text-color-secondary); line-height: 1.7; }
.actions { display: flex; gap: 8px; margin: 16px 0; }
.bot-tag { margin: 2px 4px 2px 0; }
.usage { margin-top: 32px; max-width: 80ch; }
.usage h2 { font-size: 16px; }
.usage p { color: var(--el-text-color-secondary); line-height: 1.7; }
.plaintext { display: block; margin-top: 16px; padding: 12px; word-break: break-all; background: var(--el-fill-color-light); border-radius: 4px; user-select: all; }
</style>
