<script setup lang="ts">
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
</script>
<template>
  <el-result
    icon="warning"
    :title="t('workspace.notFound')"
    :sub-title="t('workspace.notFoundHint')"
  >
    <template #extra>
      <router-link to="/">
        {{ t('menu.home') }}
      </router-link>
    </template>
  </el-result>
</template>
