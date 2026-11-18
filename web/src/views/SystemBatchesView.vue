<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import SystemBatchesPanel from '@/views/SystemBatchesPanel.vue'

const { t } = useI18n()
</script>

<template>
  <section>
    <header class="cm-page-header">
      <h2>{{ t('menu.batches') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.batches') }}
      </p>
    </header>
    <SystemBatchesPanel />
  </section>
</template>
