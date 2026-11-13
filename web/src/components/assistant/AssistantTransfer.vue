<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ApiError } from '@/api/client'
import { myAssistant, type ImportPlan } from '@/api/myAssistant'
import { saveBlob } from '@/utils/download'
import { errorMessage } from '@/utils/errors'

const emit = defineEmits<{ imported: [] }>()
const { t, te } = useI18n()
const open = ref(false), busy = ref(false), overwrite = ref(false)
const file = ref<File | null>(null)
const plan = ref<ImportPlan | null>(null)

/** 清单里会置顶的页数：置顶页每天当规则交给助理，要单独提醒。 */
const pinned = computed(() => (plan.value ? [...plan.value.create, ...plan.value.overwrite].filter((line) => line.pinned).length : 0))
const skipped = computed(() => (plan.value ? plan.value.skip.length + plan.value.followups.skip.length : 0))
const nothing = computed(() => !!plan.value && !plan.value.create.length && !plan.value.overwrite.length && !plan.value.followups.create)

/** 换了文件或「覆盖」选项，看过的清单就作废：必须重新预览。 */
function pick(chosen: File | null) { file.value = chosen; plan.value = null }
function onFile(event: Event) { pick((event.target as HTMLInputElement).files?.[0] ?? null) }
function setOverwrite(value: boolean) { overwrite.value = value; plan.value = null }
function close() { open.value = false; overwrite.value = false; pick(null) }
/** 原因代码 → 文案；后端新加的、这里还没有文案的原样显示代码。 */
function reason(code: string | undefined) {
  const key = 'assistant.notes.importReasons.' + code
  return code && te(key) ? t(key) : (code ?? '')
}

async function preview() {
  if (!file.value) { ElMessage.warning(t('assistant.notes.importChoose')); return }
  busy.value = true
  try { plan.value = await myAssistant.importArchive(file.value, { dryRun: true, overwrite: overwrite.value }) }
  catch (e) { plan.value = null; ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
/** 第二步：带着预览给的摘要确认。服务端发现清单已经变了（409）时不写，这里重新出一张清单。 */
async function confirm() {
  if (!file.value || !plan.value || nothing.value) return
  busy.value = true
  try {
    await myAssistant.importArchive(file.value, { dryRun: false, overwrite: overwrite.value, expect: plan.value.digest })
    ElMessage.success(t('assistant.notes.importDone'))
    close()
    emit('imported')
  } catch (e) {
    ElMessage.error(errorMessage(e))
    if (e instanceof ApiError && e.status === 409) await preview()
  } finally { busy.value = false }
}
async function download() {
  busy.value = true
  try {
    saveBlob(await myAssistant.exportArchive(), 'assistant-export.zip')
    ElMessage.success(t('assistant.notes.exported'))
  } catch (e) { ElMessage.error(errorMessage(e)) }
  finally { busy.value = false }
}
defineExpose({ pick, setOverwrite })
</script>
<template>
  <div class="transfer">
    <el-button
      data-test="import-open"
      @click="open = true"
    >
      {{ t('assistant.notes.importOpen') }}
    </el-button>
    <el-button
      :loading="busy && !open"
      data-test="export"
      @click="download"
    >
      {{ t('assistant.notes.exportButton') }}
    </el-button>
    <div
      v-if="open"
      class="import-panel"
      data-test="import-panel"
    >
      <p class="muted">
        {{ t('assistant.notes.importHint') }}
      </p>
      <input
        type="file"
        accept=".zip,application/zip"
        data-test="import-file"
        @change="onFile"
      >
      <el-checkbox
        :model-value="overwrite"
        data-test="import-overwrite"
        @update:model-value="setOverwrite(!!$event)"
      >
        {{ t('assistant.notes.importOverwrite') }}
      </el-checkbox>
      <div>
        <el-button
          :loading="busy"
          data-test="import-preview"
          @click="preview"
        >
          {{ t('assistant.notes.importPreview') }}
        </el-button>
        <el-button
          data-test="import-cancel"
          @click="close"
        >
          {{ t('assistant.notes.importCancel') }}
        </el-button>
      </div>
      <div
        v-if="plan"
        data-test="import-plan"
      >
        <el-alert
          v-if="pinned"
          type="warning"
          :closable="false"
          show-icon
          data-test="import-pinned-warning"
          :title="t('assistant.notes.importPinnedWarning', { count: pinned })"
        />
        <p
          v-if="plan.ignored"
          class="muted"
          data-test="import-ignored"
        >
          {{ t('assistant.notes.importIgnored', { count: plan.ignored }) }}
        </p>
        <h4>{{ t('assistant.notes.importCreate', { count: plan.create.length }) }}</h4>
        <ul data-test="import-create">
          <li
            v-for="line in plan.create"
            :key="line.path"
          >
            <code>{{ line.path }}</code>
            <el-tag
              v-if="line.pinned"
              type="warning"
              size="small"
            >
              {{ t('assistant.notes.importPinned') }}
            </el-tag>
          </li>
        </ul>
        <h4>{{ t('assistant.notes.importReplace', { count: plan.overwrite.length }) }}</h4>
        <ul data-test="import-replace">
          <li
            v-for="line in plan.overwrite"
            :key="line.path"
          >
            <code>{{ line.path }}</code>
            <el-tag
              v-if="line.pinned"
              type="warning"
              size="small"
            >
              {{ t('assistant.notes.importPinned') }}
            </el-tag>
          </li>
        </ul>
        <h4>{{ t('assistant.notes.importSkip', { count: skipped }) }}</h4>
        <ul data-test="import-skip">
          <li
            v-for="(line, i) in plan.skip"
            :key="'p' + i"
          >
            <code>{{ line.path }}</code>
            <span class="muted">{{ reason(line.reason) }}</span>
          </li>
          <li
            v-for="item in plan.followups.skip"
            :key="'f' + item.index"
          >
            <span>{{ t('assistant.notes.importFollowupSkip', { n: item.index }) }}</span>
            <span class="muted">{{ reason(item.reason) }}</span>
          </li>
        </ul>
        <p>{{ t('assistant.notes.importFollowups', { count: plan.followups.create }) }}</p>
        <p
          v-if="nothing"
          class="muted"
        >
          {{ t('assistant.notes.importNothing') }}
        </p>
        <el-button
          type="primary"
          :disabled="nothing"
          :loading="busy"
          data-test="import-confirm"
          @click="confirm"
        >
          {{ t('assistant.notes.importConfirm') }}
        </el-button>
      </div>
    </div>
  </div>
</template>
<style scoped>
.transfer { display: inline-block; margin-left: 12px; vertical-align: top; }
.import-panel { margin-top: 12px; padding: 12px; max-width: 760px; border: 1px solid var(--el-border-color); border-radius: 4px; }
.import-panel ul { list-style: none; padding: 0; }
.import-panel li { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 2px 0; }
.muted { color: var(--el-text-color-secondary); }
</style>
