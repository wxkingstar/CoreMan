<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { feishuApps, type FeishuAppOverview } from '@/api/feishuApps'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ botId: string; overview: FeishuAppOverview; disabled?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const form = reactive({ name: '', description: '', help_use: '' })
const saving = ref(false)
const uploading = ref(false)
const fileInput = ref<HTMLInputElement>()

watch(() => props.overview, (value) => {
  form.name = value.app?.name ?? ''
  form.description = value.app?.description ?? ''
  form.help_use = value.app?.help_use ?? ''
}, { immediate: true })

function useEmployee(): void {
  form.name = props.overview.employee.name
  form.description = props.overview.employee.description || props.overview.employee.name
}

async function save(): Promise<void> {
  if (!form.name.trim() || !form.description.trim()) {
    ElMessage.error(t('feishuApp.profileRequired'))
    return
  }
  saving.value = true
  try {
    await feishuApps.updateBase(props.botId, {
      language: props.overview.app?.primary_language ?? 'zh_cn',
      name: form.name.trim(),
      description: form.description.trim(),
      help_use: form.help_use.trim() || null,
    })
    ElMessage.success(t('feishuApp.savedPublish'))
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}

async function upload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 2 * 1024 * 1024) {
    ElMessage.error(t('feishuApp.avatarInvalid'))
    return
  }
  uploading.value = true
  try {
    await feishuApps.uploadAvatar(props.botId, file)
    ElMessage.success(t('feishuApp.savedPublish'))
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <section
    class="cm-panel"
    data-test="feishu-app-profile"
  >
    <h3>{{ t('feishuApp.profile') }}</h3>
    <el-form
      label-width="120px"
      :disabled="disabled"
      @submit.prevent
    >
      <el-form-item :label="t('feishuApp.avatar')">
        <img
          v-if="overview.app?.avatar_url"
          :src="overview.app.avatar_url"
          class="avatar"
          alt=""
        >
        <input
          ref="fileInput"
          type="file"
          accept="image/png,image/jpeg"
          class="file"
          data-test="avatar-input"
          @change="upload"
        >
        <el-button
          :loading="uploading"
          @click="fileInput?.click()"
        >
          {{ t('feishuApp.uploadAvatar') }}
        </el-button>
        <span class="muted">{{ t('feishuApp.avatarHint') }}</span>
      </el-form-item>
      <el-form-item :label="t('feishuApp.appName')">
        <el-input
          v-model="form.name"
          maxlength="64"
          data-test="profile-name"
        />
      </el-form-item>
      <el-form-item :label="t('feishuApp.appDescription')">
        <el-input
          v-model="form.description"
          type="textarea"
          :rows="2"
          maxlength="200"
          data-test="profile-description"
        />
      </el-form-item>
      <el-form-item :label="t('feishuApp.helpUse')">
        <el-input
          v-model="form.help_use"
          placeholder="https://"
        />
      </el-form-item>
      <el-form-item>
        <el-button
          data-test="use-employee"
          @click="useEmployee"
        >
          {{ t('feishuApp.useEmployee') }}
        </el-button>
        <el-button
          type="primary"
          :loading="saving"
          data-test="profile-save"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </el-form-item>
    </el-form>
  </section>
</template>

<style scoped>
.avatar { width: 40px; height: 40px; border-radius: 8px; object-fit: cover; margin-right: 12px; }
.file { display: none; }
.muted { margin-left: 12px; color: var(--el-text-color-secondary); font-size: 12px; }
</style>
