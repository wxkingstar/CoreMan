<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { personalCredentials, type CredentialBot, type CredentialItem } from '@/api/personalCredentials'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const rows = ref<CredentialItem[]>([])
const bots = ref<CredentialBot[]>([])
const error = ref('')
const busy = ref(false)
/** 可选的 AI 员工，加上已经绑着、但现在不在可选范围里的（例如白名单变了），标签才显示得出名字。 */
const botOptions = computed(() => {
  const options = new Map(bots.value.map(b => [b.id, b.name]))
  for (const row of rows.value) for (const b of row.bindings) if (!options.has(b.bot_id)) options.set(b.bot_id, b.bot_name)
  return [...options].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name))
})
const botName = (id: string) => botOptions.value.find(b => b.id === id)?.name ?? id
async function load() {
  busy.value = true
  try {
    const [items, options] = await Promise.all([personalCredentials.items(), personalCredentials.bots()])
    rows.value = items; bots.value = options; error.value = ''
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
/** 取消确认框不算失败。 */
async function act(action: () => Promise<unknown>, done: string) {
  busy.value = true
  try { await action(); ElMessage.success(t(done)); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
const name = (row: CredentialItem) => row.label || row.env_key
const boundIds = (row: CredentialItem) => row.bindings.map(b => b.bot_id)
/** 表单里选用时变量名与凭证自己的不同的绑定：标出在那个 AI 员工里叫什么。 */
const aliases = (row: CredentialItem) => row.bindings.filter(b => b.env_key !== row.env_key)
/** 新加的 AI 员工上同名变量已经在用别的凭证：先说清楚会被替换。 */
function replaced(row: CredentialItem, added: string[]) {
  return rows.value.flatMap(other => other.id === row.id ? [] : other.bindings
    .filter(b => added.includes(b.bot_id) && b.env_key === row.env_key)
    .map(b => t('myCredentials.replaces', { bot: b.bot_name, key: b.env_key, label: name(other) })))
}
/** 改绑定只更新这一行，不刷新整页、不禁用多选框：下拉框保持打开，可以接着选。 */
async function changeBots(row: CredentialItem, ids: string[]) {
  try {
    const added = ids.filter(id => !boundIds(row).includes(id))
    const notes = replaced(row, added)
    if (notes.length) await ElMessageBox.confirm(notes.join('\n'), t('myCredentials.replaceTitle'), { type: 'warning' })
    const updated = await personalCredentials.setBots(row.id, ids)
    rows.value = rows.value.map(r => r.id === updated.id ? updated : r)
    // 替换了别的凭证在某个 AI 员工上的同名变量：那几行也变了，静默重新拉一次。
    if (notes.length) rows.value = await personalCredentials.items()
    ElMessage.success(t('myCredentials.bound'))
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
}
const rename = (row: CredentialItem) => act(async () => {
  const { value } = await ElMessageBox.prompt(
    t('myCredentials.renamePrompt', { key: row.env_key }),
    t('myCredentials.renameTitle'),
    { inputValue: row.label, inputValidator: (v: string) => !!v && !!v.trim() && v.trim().length <= 50, inputErrorMessage: t('myCredentials.renameInvalid') },
  )
  await personalCredentials.rename(row.id, value.trim())
}, 'myCredentials.renamed')
const update = (row: CredentialItem) => act(async () => {
  const shared = row.bindings.length > 1 ? t('myCredentials.updateShared', { bots: row.bindings.map(b => b.bot_name).join('、') }) : ''
  const { value } = await ElMessageBox.prompt(
    t('myCredentials.updatePrompt') + shared,
    t('myCredentials.updateTitle', { label: name(row) }),
    { inputType: row.secret ? 'password' : 'text', inputValidator: (v: string) => !!v && !!v.trim() },
  )
  await personalCredentials.updateItem(row.id, value)
}, 'myCredentials.updated')
const remove = (row: CredentialItem) => act(async () => {
  await ElMessageBox.confirm(t('myCredentials.confirmDelete', { label: name(row), key: row.env_key }), t('myCredentials.delete'), { type: 'warning' })
  await personalCredentials.removeItem(row.id)
}, 'myCredentials.deleted')
onMounted(load)
</script>
<template>
  <section class="credentials">
    <h1>{{ t('menu.myCredentials') }}</h1>
    <p>{{ t('myCredentials.intro') }}</p>
    <el-button
      :loading="busy"
      @click="load"
    >
      {{ t('myCredentials.refresh') }}
    </el-button>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
    />
    <el-empty
      v-if="!busy && !rows.length"
      :description="t('myCredentials.empty')"
    />
    <el-table
      v-else
      class="table"
      :data="rows"
      row-key="id"
    >
      <el-table-column
        :label="t('myCredentials.label')"
        prop="label"
        min-width="120"
      />
      <el-table-column
        :label="t('myCredentials.key')"
        min-width="150"
      >
        <template #default="{ row }">
          <code>{{ row.env_key }}</code>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('myCredentials.value')"
        min-width="120"
      >
        <template #default="{ row }">
          {{ row.secret ? t('myCredentials.hidden') : row.value }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('myCredentials.bots')"
        min-width="260"
      >
        <template #default="{ row }">
          <el-select
            :model-value="boundIds(row)"
            :data-test="'bots-' + row.id"
            multiple
            filterable
            :reserve-keyword="false"
            :disabled="busy"
            :placeholder="t('myCredentials.unbound')"
            @update:model-value="(ids: string[]) => changeBots(row, ids)"
          >
            <el-option
              v-for="bot in botOptions"
              :key="bot.id"
              :label="bot.name"
              :value="bot.id"
            />
          </el-select>
          <div
            v-for="b in aliases(row)"
            :key="b.bot_id"
            class="alias"
          >
            {{ t('myCredentials.alias', { bot: botName(b.bot_id), key: b.env_key }) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('myCredentials.updatedAt')">
        <template #default="{ row }">
          {{ formatDateTime(row.updated_at) }}
        </template>
      </el-table-column>
      <el-table-column :label="t('myCredentials.lastUsedAt')">
        <template #default="{ row }">
          {{ row.last_used_at ? formatDateTime(row.last_used_at) : t('myCredentials.never') }}
        </template>
      </el-table-column>
      <el-table-column width="190">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            :data-test="'rename-' + row.id"
            @click="rename(row)"
          >
            {{ t('myCredentials.rename') }}
          </el-button>
          <el-button
            link
            type="primary"
            :data-test="'update-' + row.id"
            @click="update(row)"
          >
            {{ t('myCredentials.update') }}
          </el-button>
          <el-button
            link
            type="danger"
            :data-test="'delete-' + row.id"
            @click="remove(row)"
          >
            {{ t('myCredentials.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>
<style scoped>
.table { margin-top: 16px; }
.alias { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 4px; }
</style>
