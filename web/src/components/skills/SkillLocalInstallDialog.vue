<script setup lang="ts">
/** 本地安装：任何登录用户都能拿到带时效的安装命令；平台管理员还能查看技能环境变量的明文 export 语句。 */
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { skills, type Skill } from '@/api/skills'
import { useAuthStore } from '@/stores/auth'
import { errorMessage } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'

const { t } = useI18n()
const auth = useAuthStore()
const platformAdmin = computed(() => auth.user?.role === 'platform_admin')
const visible = ref(false), loading = ref(false), revealing = ref(false)
const row = ref<Skill | null>(null), command = ref(''), expiresAt = ref(''), exports = ref('')
const form = reactive<{ selected_env_groups: string[]; data_source: string | null }>({ selected_env_groups: [], data_source: null })
const databaseOptions = computed(() => Object.entries(row.value?.selectable_env_groups ?? {}).map(([value, label]) => ({ value, label })))
const sourceOptions = computed(() => Object.entries(row.value?.data_sources ?? {}).map(([value, label]) => ({ value, label })))

async function open(target: Skill) {
  row.value = target; command.value = ''; expiresAt.value = ''; exports.value = ''
  Object.assign(form, { selected_env_groups: [], data_source: target.default_data_source })
  visible.value = true
  if (target.install_type !== 'git' || !target.enabled) return
  loading.value = true
  try { const data = await skills.localInstall(target); command.value = data.command; expiresAt.value = data.expires_at }
  catch (e) { ElMessage.error(errorMessage(e)) }
  finally { loading.value = false }
}
async function reveal() {
  if (!row.value) return
  revealing.value = true
  try { exports.value = (await skills.envExports(row.value, form)).text }
  catch (e) { ElMessage.error(errorMessage(e)) }
  finally { revealing.value = false }
}
async function copy(text: string) {
  try { await navigator.clipboard.writeText(text); ElMessage.success(t('common.copied')) }
  catch { ElMessage.info(t('skillEditor.copyManually')) }
}
function closed() { command.value = ''; exports.value = '' }
defineExpose({ open })
</script>
<template>
  <el-dialog
    v-model="visible"
    :title="t('skillEditor.localInstallTitle', { name: row?.name ?? '' })"
    width="680px"
    destroy-on-close
    @closed="closed"
  >
    <template v-if="row">
      <section>
        <p
          v-if="row.install_type !== 'git'"
          class="hint"
        >
          {{ t('skillEditor.localInstallMcp') }}
        </p>
        <p
          v-else-if="!row.enabled"
          class="hint"
        >
          {{ t('skillEditor.localInstallDisabled') }}
        </p>
        <div
          v-else-if="loading"
          v-loading="true"
          class="loading"
        />
        <template v-else-if="command">
          <p class="hint">
            {{ t('skillEditor.localInstallHint', { name: row.name }) }}
          </p>
          <el-input
            :model-value="command"
            type="textarea"
            :rows="3"
            readonly
            data-test="local-install-command"
          />
          <div class="actions">
            <small class="hint">{{ t('skillEditor.localInstallExpires', { time: formatDateTime(expiresAt) }) }}</small>
            <el-button
              type="primary"
              data-test="local-install-copy"
              @click="copy(command)"
            >
              {{ t('skillEditor.copyCommand') }}
            </el-button>
          </div>
        </template>
      </section>
      <section
        v-if="platformAdmin"
        class="env"
        data-test="env-export"
      >
        <h4>{{ t('skillEditor.envExportTitle') }}</h4>
        <p class="hint">
          {{ t('skillEditor.envExportHint') }}
        </p>
        <el-form label-position="top">
          <el-form-item
            v-if="databaseOptions.length"
            :label="t('skill.databases')"
          >
            <el-checkbox-group v-model="form.selected_env_groups">
              <el-checkbox
                v-for="option in databaseOptions"
                :key="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item
            v-if="row.data_sources"
            :label="t('skill.dataSource')"
          >
            <el-radio-group v-model="form.data_source">
              <el-radio
                v-for="option in sourceOptions"
                :key="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </el-radio>
            </el-radio-group>
          </el-form-item>
        </el-form>
        <el-button
          :loading="revealing"
          data-test="env-export-reveal"
          @click="reveal"
        >
          {{ t('skillEditor.envExportShow') }}
        </el-button>
        <template v-if="exports">
          <el-input
            :model-value="exports"
            type="textarea"
            :autosize="{ minRows: 4, maxRows: 16 }"
            readonly
            class="exports"
            data-test="env-export-text"
          />
          <div class="actions">
            <span />
            <el-button @click="copy(exports)">
              {{ t('skillEditor.copyExports') }}
            </el-button>
          </div>
        </template>
      </section>
    </template>
    <template #footer>
      <el-button @click="visible = false">
        {{ t('common.close') }}
      </el-button>
    </template>
  </el-dialog>
</template>
<style scoped>
.hint { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.7; margin: 0 0 12px; overflow-wrap: anywhere; }
small.hint { margin: 0; }
.loading { min-height: 80px; }
.actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; }
.env { border-top: 1px solid var(--el-border-color-lighter); margin-top: 20px; padding-top: 16px; }
.env h4 { margin: 0 0 6px; }
.exports { margin-top: 12px; }
.exports :deep(textarea) { font-family: var(--el-font-family-mono, ui-monospace, monospace); }
</style>
