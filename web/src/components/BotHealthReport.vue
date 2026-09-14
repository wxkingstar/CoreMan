<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { healthReport } from '@/api/healthReport'
const props = defineProps<{ botId: string }>()
const { t } = useI18n()
const visible = ref(false), busy = ref(false), completed = ref(false)
const content = ref(''), error = ref(''), tool = ref('')
let controller: AbortController | null = null
async function run() {
  if (busy.value) return
  content.value = ''; error.value = ''; completed.value = false; tool.value = ''; busy.value = true
  controller = new AbortController()
  try { await healthReport(props.botId, controller.signal, event => { if (event.kind === 'text') content.value += event.text ?? ''; if (event.kind === 'tool') tool.value = event.name ?? ''; if (event.kind === 'done') completed.value = true }) }
  catch (e) { error.value = controller.signal.aborted ? t('healthReport.cancelled') : (e instanceof Error ? e.message : String(e)) }
  finally { busy.value = false; controller = null; tool.value = '' }
}
function stop() { controller?.abort() }
onBeforeUnmount(stop)
</script>
<template>
  <el-button @click="visible = true">
    {{ t('healthReport.title') }}
  </el-button>
  <el-dialog
    v-model="visible"
    :title="t('healthReport.title')"
    width="850px"
    @close="stop"
  >
    <el-alert
      :title="t('healthReport.hint')"
      type="info"
      :closable="false"
    />
    <div class="toolbar">
      <el-button
        type="primary"
        :loading="busy"
        @click="run"
      >
        {{ t('healthReport.run') }}
      </el-button><el-button
        v-if="busy"
        @click="stop"
      >
        {{ t('healthReport.stop') }}
      </el-button><el-tag
        v-if="completed"
        type="success"
      >
        {{ t('healthReport.done') }}
      </el-tag><span v-if="tool">{{ t('healthReport.tool', { name: tool }) }}</span>
    </div>
    <el-alert
      v-if="error"
      :title="error"
      type="warning"
      :closable="false"
    />
    <pre class="report">{{ content || t('healthReport.empty') }}</pre>
  </el-dialog>
</template>
<style scoped>.toolbar { display: flex; align-items: center; gap: 12px; margin: 16px 0; }.report { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 60vh; overflow-y: auto; line-height: 1.7; padding: 16px; background: var(--el-fill-color-light); }</style>
