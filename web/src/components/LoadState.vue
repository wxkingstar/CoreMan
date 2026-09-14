<script setup lang="ts">
import { useI18n } from 'vue-i18n'
defineProps<{ loading?: boolean; error?: string | null; empty?: boolean }>()
defineEmits<{ retry: [] }>()
const { t } = useI18n()
</script>
<template>
  <div
    class="cm-state"
    aria-live="polite"
    :aria-busy="loading"
  >
    <el-skeleton
      v-if="loading"
      :rows="3"
      animated
    />
    <el-alert
      v-else-if="error"
      :title="t('common.loadFailed')"
      type="error"
      :closable="false"
      show-icon
    >
      <p>{{ error }}</p><el-button @click="$emit('retry')">
        {{ t('workspace.retry') }}
      </el-button>
    </el-alert>
    <el-empty
      v-else-if="empty"
      :description="t('workspace.empty')"
      :image-size="64"
    />
  </div>
</template>
