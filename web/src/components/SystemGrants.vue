<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { systems, type BusinessSystem } from '@/api/infrastructure'
const props = defineProps<{ botId: string }>()
const emit = defineEmits<{ saved: [] }>()
const { t } = useI18n()
const loading = ref(false), busy = ref(false), version = ref(0), loadError = ref('')
const keys = ref<string[]>([]), saved = ref<string[]>([]), options = ref<BusinessSystem[]>([])
const dirty = computed(() => keys.value.length !== saved.value.length || keys.value.some(k => !saved.value.includes(k)))
async function load() {
  loading.value = true; loadError.value = ''
  try {
    const grants = await systems.grants(props.botId)
    keys.value = [...grants.system_keys]; saved.value = grants.system_keys; version.value = grants.version
    const all: BusinessSystem[] = []
    for (let page = 1; ; page++) { const data = await systems.list(page); all.push(...data.items); if (all.length >= data.total || !data.items.length) break }
    options.value = all.filter(s => s.enabled && (s.allowed_bot_ids === null || s.allowed_bot_ids.includes(props.botId)))
  } catch (e) { loadError.value = errorMessage(e); ElMessage.error(loadError.value) } finally { loading.value = false }
}
async function save() {
  if (busy.value) return
  busy.value = true
  try {
    const grants = await systems.saveGrants(props.botId, keys.value, version.value)
    saved.value = grants.system_keys; keys.value = [...grants.system_keys]; version.value = grants.version
    emit('saved'); ElMessage.success(t('common.saved'))
  } catch (e) { ElMessage.error(errorMessage(e)) } finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <section
    v-loading="loading"
    class="cm-panel"
  >
    <div class="grants-head">
      <div>
        <h3>{{ t('infra.systemGrants') }}<span class="grants-count">（{{ saved.length }}）</span></h3>
        <p class="grants-hint">
          {{ t('infra.grantsHint') }}
        </p>
      </div>
      <div class="grants-actions">
        <el-button
          :disabled="!dirty || busy"
          @click="keys = [...saved]"
        >
          {{ t('common.reset') }}
        </el-button><el-button
          type="primary"
          :loading="busy"
          :disabled="!dirty"
          data-test="save-grants"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </div>
    </div>
    <LoadState
      :error="loadError"
      @retry="load"
    />
    <el-checkbox-group
      v-if="options.length"
      v-model="keys"
      class="grants-grid"
    >
      <el-checkbox
        v-for="system in options"
        :key="system.key"
        :value="system.key"
        border
        class="grant-option"
      >
        <span class="grant-name">
          {{ system.name }}
          <el-tag
            v-if="system.default_for_all_bots"
            size="small"
            type="info"
          >{{ t('infra.defaultAccess') }}</el-tag>
        </span>
        <span
          v-if="system.description"
          class="grant-desc"
        >{{ system.description }}</span>
      </el-checkbox>
    </el-checkbox-group>
    <p
      v-else-if="!loading && !loadError"
      class="grants-hint"
    >
      {{ t('infra.noSystems') }}
    </p>
  </section>
</template>
<style scoped>
.grants-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 16px; }
.grants-head h3 { margin: 0 0 6px; }
.grants-count { color: var(--el-text-color-secondary); font-weight: 400; }
.grants-hint { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.7; }
.grants-actions { display: flex; gap: 8px; flex-shrink: 0; }
.grants-actions .el-button { margin-left: 0; }
.grants-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.grant-option.el-checkbox { display: flex; align-items: flex-start; height: auto; margin: 0; padding: 14px 16px; border-radius: 8px; }
.grant-option :deep(.el-checkbox__input) { margin-top: 3px; }
.grant-option :deep(.el-checkbox__label) { display: flex; flex-direction: column; gap: 4px; min-width: 0; padding-left: 10px; white-space: normal; }
.grant-name { display: flex; align-items: center; gap: 8px; font-weight: 500; overflow-wrap: anywhere; }
.grant-desc { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
@media (max-width: 767px) { .grants-head { flex-direction: column; gap: 12px; } }
</style>
