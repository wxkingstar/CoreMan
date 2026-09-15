<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { skills, type Source } from '@/api/skills'

const emit = defineEmits<{ saved: [] }>()
const { t } = useI18n()
const busy = ref(false), sourceVisible = ref(false)
const selectedSource = ref<Source | null>(null)
const sourceForm = reactive({ key: '', label: '', git_url: '', categories: {} as Record<string, string>, sort_order: 0, access_token: '', remove_access_token: false })
const fail = (e: unknown) => ElMessage.error(errorMessage(e))

/** 打开来源对话框：已保存的项目令牌不回显，只能保留、替换或移除。 */
function open(row: Source | null) { selectedSource.value = row; Object.assign(sourceForm, { key: row?.key ?? '', label: row?.label ?? '', git_url: row?.git_url ?? '', categories: row?.categories ?? {}, sort_order: row?.sort_order ?? 0, access_token: '', remove_access_token: false }); sourceVisible.value = true }
async function saveSource() { busy.value = true; try { await skills.sourceSave(selectedSource.value, { ...sourceForm, git_url: sourceForm.git_url || null }); sourceVisible.value = false; sourceForm.access_token = ''; emit('saved') } catch (e) { fail(e) } finally { busy.value = false } }
defineExpose({ open, saveSource, sourceForm })
</script>

<template>
  <el-dialog
    v-model="sourceVisible"
    :close-on-click-modal="false"
    :title="t(selectedSource ? 'skillEditor.editSource' : 'skillEditor.newSource')"
    width="680px"
    @closed="sourceForm.access_token = ''"
  >
    <p class="hint dialog-intro">
      {{ t('skillEditor.sourceDialogHint') }}
    </p>
    <el-form
      label-position="top"
      :disabled="busy"
    >
      <div class="form-grid">
        <el-form-item
          :label="t('common.name')"
          required
        >
          <el-input
            v-model="sourceForm.label"
            :placeholder="t('skillEditor.sourceLabelHint')"
          />
        </el-form-item>
        <el-form-item
          :label="t('infra.key')"
          required
        >
          <el-input
            v-model="sourceForm.key"
            placeholder="tools-marketplace"
          />
        </el-form-item>
        <el-form-item
          class="full-width"
          :label="t('skill.repository')"
        >
          <el-input
            v-model="sourceForm.git_url"
            placeholder="https://github.com/example/skills.git"
          /><span class="hint">{{ t('skillEditor.repositoryHint') }}</span>
        </el-form-item>
        <el-form-item
          class="full-width"
          :label="t('skillEditor.projectToken')"
        >
          <el-input
            v-model="sourceForm.access_token"
            type="password"
            show-password
            autocomplete="new-password"
            :disabled="sourceForm.remove_access_token"
            :placeholder="t(selectedSource?.has_access_token ? 'skillEditor.tokenPreserve' : 'skillEditor.tokenPlaceholder')"
          />
          <span class="hint">{{ t('skillEditor.tokenHint') }}</span>
          <el-checkbox
            v-if="selectedSource?.has_access_token"
            v-model="sourceForm.remove_access_token"
            @change="sourceForm.access_token = ''"
          >
            {{ t('skillEditor.removeToken') }}
          </el-checkbox>
        </el-form-item>
        <el-form-item :label="t('infra.sortOrder')">
          <el-input-number
            v-model="sourceForm.sort_order"
            :min="0"
            :max="10000"
          />
        </el-form-item>
      </div>
    </el-form>
    <template #footer>
      <el-button @click="sourceVisible = false">
        {{ t('common.cancel') }}
      </el-button><el-button
        type="primary"
        :loading="busy"
        @click="saveSource"
      >
        {{ t('skillEditor.saveSource') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped src="./skillForm.css"></style>
