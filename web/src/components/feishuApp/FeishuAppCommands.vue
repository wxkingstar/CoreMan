<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { feishuApps, type FeishuAppOverview, type FeishuSlashCommand } from '@/api/feishuApps'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ botId: string; overview: FeishuAppOverview }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
/** 飞书图标库中的常用图标（完整列表见飞书斜杠指令文档）。 */
const ICONS = ['skill_outlined', 'ai-agent_outlined', 'chat-ai_outlined', 'ai-doc_outlined', 'search-ai_outlined', 'meeting-ai_outlined', 'calendar-line_outlined', 'code_outlined', 'database_outlined', 'robot_outlined', 'add-chat-ai_outlined', 'clear_outlined', 'chat_outlined', 'explanation-ai_outlined']

const dialogVisible = ref(false)
const addingDefaults = ref(false)

async function addDefaults(): Promise<void> {
  addingDefaults.value = true
  try {
    const { created } = await feishuApps.addDefaultCommands(props.botId)
    if (created.length) ElMessage.success(t('feishuApp.defaultCommandsAdded', { commands: created.map(c => `/${c}`).join(' ') }))
    else ElMessage.info(t('feishuApp.defaultCommandsPresent'))
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    addingDefaults.value = false
  }
}
const editing = ref<FeishuSlashCommand | null>(null)
const form = reactive({ command: '', description: '', icon_key: 'skill_outlined' })
const saving = ref(false)

function open(row: FeishuSlashCommand | null): void {
  editing.value = row
  form.command = row?.command ?? ''
  form.description = row?.description ?? ''
  form.icon_key = row?.icon_key ?? 'skill_outlined'
  dialogVisible.value = true
}

async function save(): Promise<void> {
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(form.command) || !form.description.trim()) {
    ElMessage.error(t('feishuApp.commandInvalid'))
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await feishuApps.updateCommand(props.botId, editing.value.command_id, { description: form.description.trim(), icon_key: form.icon_key })
    } else {
      await feishuApps.createCommand(props.botId, { command: form.command, description: form.description.trim(), icon_key: form.icon_key })
    }
    ElMessage.success(t('feishuApp.commandSaved'))
    dialogVisible.value = false
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}

async function remove(row: FeishuSlashCommand): Promise<void> {
  try {
    await ElMessageBox.confirm(t('feishuApp.commandDeleteConfirm', { command: row.command }), t('common.delete'), { type: 'warning' })
  } catch {
    return
  }
  try {
    await feishuApps.deleteCommand(props.botId, row.command_id)
    ElMessage.success(t('feishuApp.commandSaved'))
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
</script>

<template>
  <section
    class="cm-panel"
    data-test="feishu-app-commands"
  >
    <h3>{{ t('feishuApp.commands') }}</h3>
    <p class="cm-page-intro">
      {{ t('feishuApp.commandsHint') }}
    </p>
    <el-alert
      v-if="overview.errors.slash_commands"
      type="warning"
      :closable="false"
      :title="t('feishuApp.commandsError', { code: overview.errors.slash_commands })"
    />
    <template v-else>
      <el-table
        :data="overview.slash_commands ?? []"
        size="small"
        :empty-text="t('feishuApp.commandsEmpty')"
      >
        <el-table-column
          :label="t('feishuApp.command')"
          width="180"
        >
          <template #default="{ row }">
            <code>/{{ row.command }}</code>
          </template>
        </el-table-column>
        <el-table-column
          prop="description"
          :label="t('feishuApp.commandDescription')"
          min-width="200"
        />
        <el-table-column
          prop="icon_key"
          :label="t('feishuApp.commandIcon')"
          width="180"
        />
        <el-table-column width="140">
          <template #default="{ row }">
            <el-button
              text
              type="primary"
              @click="open(row)"
            >
              {{ t('common.edit') }}
            </el-button>
            <el-button
              text
              type="danger"
              @click="remove(row)"
            >
              {{ t('common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button
        class="add"
        data-test="command-add"
        @click="open(null)"
      >
        {{ t('feishuApp.commandAdd') }}
      </el-button>
      <el-button
        class="add"
        :loading="addingDefaults"
        data-test="command-defaults"
        @click="addDefaults"
      >
        {{ t('feishuApp.defaultCommands') }}
      </el-button>
    </template>
    <el-dialog
      v-model="dialogVisible"
      :title="editing ? t('feishuApp.commandEdit') : t('feishuApp.commandAdd')"
      width="460px"
      append-to-body
    >
      <el-form
        label-width="90px"
        @submit.prevent
      >
        <el-form-item :label="t('feishuApp.command')">
          <el-input
            v-model="form.command"
            :disabled="!!editing"
            maxlength="32"
            data-test="command-name"
          >
            <template #prepend>
              /
            </template>
          </el-input>
        </el-form-item>
        <el-form-item :label="t('feishuApp.commandDescription')">
          <el-input
            v-model="form.description"
            maxlength="100"
            data-test="command-description"
          />
        </el-form-item>
        <el-form-item :label="t('feishuApp.commandIcon')">
          <el-select v-model="form.icon_key">
            <el-option
              v-for="icon in ICONS"
              :key="icon"
              :value="icon"
              :label="icon"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          :loading="saving"
          data-test="command-save"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.add { margin-top: 12px; }
</style>
