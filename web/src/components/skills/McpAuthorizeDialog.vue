<script setup lang="ts">
/** MCP 技能的 OAuth 授权：运行时的 CLI 发起登录，用户在浏览器授权后把回调地址交回运行时。 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { skills, type Installed, type McpLogin } from '@/api/skills'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ botId: string }>()
const emit = defineEmits<{ authorized: [] }>()
const { t } = useI18n()
const visible = ref(false), starting = ref(false), busy = ref(false)
const row = ref<Installed | null>(null), login = ref<McpLogin | null>(null), callback = ref('')

async function open(target: Installed) {
  row.value = target; login.value = null; callback.value = ''; visible.value = true; starting.value = true
  try { login.value = await skills.mcpLoginStart(props.botId, target.skill_id) }
  catch (e) { ElMessage.error(errorMessage(e)); visible.value = false }
  finally { starting.value = false }
}
async function complete() {
  if (!row.value || !login.value || !callback.value.trim() || busy.value) return
  busy.value = true
  try {
    await skills.mcpLoginComplete(props.botId, row.value.skill_id, { session_id: login.value.session_id, callback_url: callback.value.trim() })
    ElMessage.success(t('skillEditor.mcpDone')); visible.value = false; emit('authorized')
  } catch (e) { ElMessage.error(errorMessage(e)) } finally { busy.value = false }
}
defineExpose({ open })
</script>
<template>
  <el-dialog
    v-model="visible"
    :close-on-click-modal="false"
    :title="t('skillEditor.mcpDialogTitle', { name: row?.name ?? '' })"
    width="600px"
    destroy-on-close
  >
    <p class="hint">
      {{ t('skillEditor.mcpIdentityHint') }}
    </p>
    <div
      v-if="starting"
      v-loading="true"
      class="starting"
    >
      {{ t('skillEditor.mcpStarting') }}
    </div>
    <template v-else-if="login">
      <p>{{ t('skillEditor.mcpStep1') }}</p>
      <el-button
        type="primary"
        tag="a"
        :href="login.authorization_url"
        target="_blank"
        rel="noopener noreferrer"
        data-test="mcp-open"
      >
        {{ t('skillEditor.mcpOpen') }}
      </el-button>
      <p class="step">
        {{ t('skillEditor.mcpStep2') }}
      </p>
      <el-input
        v-model="callback"
        type="textarea"
        :rows="3"
        placeholder="http://127.0.0.1:…/callback?code=…"
        data-test="mcp-callback"
      />
    </template>
    <template #footer>
      <el-button @click="visible = false">
        {{ t('common.cancel') }}
      </el-button>
      <el-button
        type="primary"
        :loading="busy"
        :disabled="!login || !callback.trim()"
        data-test="mcp-complete"
        @click="complete"
      >
        {{ t('skillEditor.mcpComplete') }}
      </el-button>
    </template>
  </el-dialog>
</template>
<style scoped>
.hint { color: var(--el-text-color-secondary); font-size: 13px; margin: 0 0 12px; }
.starting { min-height: 80px; display: flex; align-items: center; justify-content: center; color: var(--el-text-color-secondary); }
.step { margin: 16px 0 8px; }
</style>
