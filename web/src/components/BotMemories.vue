<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { memories, type MemoryInput, type MemoryRow } from '@/api/memories'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{ botId: string }>()
const { t } = useI18n()
const loadError = ref('')
const visible = ref(false)
const editing = ref(false)
const loading = ref(false)
const busy = ref(false)
const deleted = ref(false)
const rows = ref<MemoryRow[]>([])
const selected = ref<MemoryRow | null>(null)
const form = reactive<MemoryInput>({ file_name: '', content: '' })
const fail = (error: unknown) => ElMessage.error(errorMessage(error))
async function reload() {
  loadError.value = ''
  loading.value = true
  try { rows.value = await memories.list(props.botId, deleted.value) } catch (error) { loadError.value = errorMessage(error); fail(error) } finally { loading.value = false }
}
watch([visible, deleted], () => { if (visible.value) void reload() })
async function edit(row?: MemoryRow) {
  try {
    const data = row ? await memories.get(props.botId, row.id) : null
    selected.value = data
    Object.assign(form, { file_name: data?.file_name ?? '', content: data?.content ?? '', name: data?.name ?? null, description: data?.description ?? null, type: data?.type ?? null })
    editing.value = true
  } catch (error) { fail(error) }
}
async function save() {
  if (!form.file_name.trim() || !form.file_name.endsWith('.md')) { ElMessage.warning(t('memory.filenameHint')); return }
  busy.value = true
  try {
    if (selected.value) await memories.update(props.botId, selected.value.id, form, selected.value.version)
    else await memories.create(props.botId, form)
    editing.value = false
    ElMessage.success(t('common.saved'))
    await reload()
  } catch (error) { fail(error) } finally { busy.value = false }
}
async function remove(row: MemoryRow) {
  try { await ElMessageBox.confirm(t('memory.deleteHint'), t('common.delete'), { type: 'warning' }) } catch { return }
  busy.value = true
  try { await memories.remove(props.botId, row.id, row.version); await reload() } catch (error) { fail(error) } finally { busy.value = false }
}
async function sync(operation: 'collect' | 'deploy') {
  if (operation === 'deploy') {
    try { await ElMessageBox.confirm(t('memory.deployHint'), t('memory.deploy'), { type: 'warning' }) } catch { return }
  }
  busy.value = true
  try { const result = await memories[operation](props.botId); ElMessage.success(t('memory.done', { count: result.count })); await reload() } catch (error) { fail(error) } finally { busy.value = false }
}
</script>

<template>
  <el-button
    data-test="memories-open"
    @click="visible = true"
  >
    {{ t('memory.title') }}
  </el-button>
  <el-dialog
    v-model="visible"
    :title="t('memory.title')"
    width="900px"
    destroy-on-close
  >
    <el-alert
      :title="t('memory.hint')"
      type="info"
      :closable="false"
      show-icon
    />
    <div class="memory-toolbar">
      <el-button
        type="primary"
        :disabled="busy"
        @click="edit()"
      >
        {{ t('common.create') }}
      </el-button>
      <el-button
        :loading="busy"
        @click="sync('collect')"
      >
        {{ t('memory.collect') }}
      </el-button>
      <el-button
        :disabled="busy"
        @click="sync('deploy')"
      >
        {{ t('memory.deploy') }}
      </el-button>
      <el-checkbox v-model="deleted">
        {{ t('memory.showDeleted') }}
      </el-checkbox>
    </div>
    <LoadState
      :error="loadError"
      @retry="reload"
    />
    <el-table
      v-loading="loading"
      :data="rows"
      max-height="480"
      row-key="id"
    >
      <el-table-column
        prop="file_name"
        :label="t('memory.filename')"
        min-width="170"
      />
      <el-table-column
        :label="t('memory.modified')"
        min-width="170"
      >
        <template #default="{ row }">
          {{ formatDateTime(row.file_mtime) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('memory.status')"
        width="100"
      >
        <template #default="{ row }">
          <el-tag :type="row.deleted_at ? 'info' : 'success'">
            {{ row.deleted_at ? t('memory.deleted') : t('memory.saved') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('common.actions')"
        width="180"
      >
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            :disabled="busy"
            :data-test="`memory-edit-${row.id}`"
            @click="edit(row)"
          >
            {{ row.deleted_at ? t('memory.restore') : t('common.edit') }}
          </el-button>
          <el-button
            v-if="!row.deleted_at"
            link
            type="danger"
            :disabled="busy"
            @click="remove(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>
  <el-dialog
    v-model="editing"
    :title="t('memory.edit')"
    width="800px"
    append-to-body
    :close-on-click-modal="false"
  >
    <el-form label-position="top">
      <el-form-item :label="t('memory.filename')">
        <el-input
          v-model="form.file_name"
          :disabled="!!selected"
          maxlength="200"
          :placeholder="t('memory.filenameHint')"
        />
      </el-form-item>
      <el-form-item :label="t('memory.content')">
        <el-input
          v-model="form.content"
          type="textarea"
          :rows="18"
          maxlength="262144"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button
        :disabled="busy"
        @click="editing = false"
      >
        {{ t('common.cancel') }}
      </el-button><el-button
        type="primary"
        :loading="busy"
        data-test="memory-save"
        @click="save"
      >
        {{ t('common.save') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.memory-toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 16px 0; }
.memory-toolbar .el-button + .el-button { margin-left: 0; }
</style>
