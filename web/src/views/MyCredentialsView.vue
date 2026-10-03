<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { personalCredentials, type PersonalCredential } from '@/api/personalCredentials'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const rows = ref<PersonalCredential[]>([])
const error = ref('')
const busy = ref(false)
const groups = computed(() => {
  const byBot = new Map<string, { id: string; bot: string; items: PersonalCredential[] }>()
  for (const row of rows.value) {
    const group = byBot.get(row.bot_id) ?? { id: row.bot_id, bot: row.bot_name, items: [] }
    group.items.push(row)
    byBot.set(row.bot_id, group)
  }
  return [...byBot.values()]
})
async function load() {
  busy.value = true
  try { rows.value = await personalCredentials.list(); error.value = '' }
  catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
/** 取消确认框不算失败。 */
async function act(action: () => Promise<unknown>, done: string) {
  busy.value = true
  try { await action(); ElMessage.success(t(done)); await load() }
  catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
const name = (row: PersonalCredential) => row.label || row.env_key
const update = (row: PersonalCredential) => act(async () => {
  const { value } = await ElMessageBox.prompt(
    t('myCredentials.updatePrompt'),
    t('myCredentials.updateTitle', { label: name(row) }),
    { inputType: row.secret ? 'password' : 'text', inputValidator: (v: string) => !!v && !!v.trim() },
  )
  await personalCredentials.update(row.bot_id, row.env_key, value)
}, 'myCredentials.updated')
const remove = (row: PersonalCredential) => act(async () => {
  await ElMessageBox.confirm(t('myCredentials.confirmDelete', { label: name(row), key: row.env_key }), t('myCredentials.delete'), { type: 'warning' })
  await personalCredentials.remove(row.bot_id, row.env_key)
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
    <article
      v-for="group in groups"
      :key="group.id"
      class="group"
    >
      <h2>{{ group.bot }}</h2>
      <el-table
        :data="group.items"
        row-key="env_key"
      >
        <el-table-column
          :label="t('myCredentials.label')"
          prop="label"
        />
        <el-table-column :label="t('myCredentials.key')">
          <template #default="{ row }">
            <code>{{ row.env_key }}</code>
          </template>
        </el-table-column>
        <el-table-column :label="t('myCredentials.value')">
          <template #default="{ row }">
            {{ row.secret ? t('myCredentials.hidden') : row.value }}
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
        <el-table-column width="160">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :data-test="'update-' + row.env_key"
              @click="update(row)"
            >
              {{ t('myCredentials.update') }}
            </el-button>
            <el-button
              link
              type="danger"
              :data-test="'delete-' + row.env_key"
              @click="remove(row)"
            >
              {{ t('myCredentials.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </article>
  </section>
</template>
<style scoped>
.group { margin-top: 24px; }
</style>
