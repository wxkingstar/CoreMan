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
// 允许平台代理执行写操作的系统：只对令牌交付方式为「平台代理」且已勾选的系统有意义
const writes = ref<string[]>([]), savedWrites = ref<string[]>([])
const same = (a: string[], b: string[]) => a.length === b.length && a.every(k => b.includes(k))
const dirty = computed(() => !same(keys.value, saved.value) || !same(writes.value.filter(k => keys.value.includes(k)), savedWrites.value))
function setWrite(key: string, on: boolean) { writes.value = on ? [...new Set([...writes.value, key])] : writes.value.filter(k => k !== key) }
async function load() {
  loading.value = true; loadError.value = ''
  try {
    const grants = await systems.grants(props.botId)
    keys.value = [...grants.system_keys]; saved.value = grants.system_keys; version.value = grants.version
    writes.value = [...grants.write_keys]; savedWrites.value = grants.write_keys
    const all: BusinessSystem[] = []
    for (let page = 1; ; page++) { const data = await systems.list(page); all.push(...data.items); if (all.length >= data.total || !data.items.length) break }
    options.value = all.filter(s => s.enabled && (s.allowed_bot_ids === null || s.allowed_bot_ids.includes(props.botId)))
  } catch (e) { loadError.value = errorMessage(e); ElMessage.error(loadError.value) } finally { loading.value = false }
}
async function save() {
  if (busy.value) return
  busy.value = true
  try {
    const grants = await systems.saveGrants(props.botId, keys.value, version.value, writes.value.filter(k => keys.value.includes(k)))
    saved.value = grants.system_keys; keys.value = [...grants.system_keys]; version.value = grants.version
    savedWrites.value = grants.write_keys; writes.value = [...grants.write_keys]
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
          @click="keys = [...saved]; writes = [...savedWrites]"
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
      <div
        v-for="system in options"
        :key="system.key"
        class="grant-cell"
        :class="{ 'is-checked': keys.includes(system.key) }"
      >
        <el-checkbox
          :value="system.key"
          class="grant-option"
        >
          <span class="grant-name">
            <span class="grant-title">{{ system.name }}</span>
            <el-tag
              v-if="system.default_for_all_bots"
              size="small"
              type="info"
            >{{ t('infra.defaultAccess') }}</el-tag>
          </span>
          <span
            v-if="system.description"
            class="grant-desc"
            :title="system.description"
          >{{ system.description }}</span>
        </el-checkbox>
        <div
          v-if="system.token_delivery === 'proxy' && keys.includes(system.key)"
          class="grant-write"
        >
          <el-switch
            :model-value="writes.includes(system.key)"
            :data-test="`allow-write-${system.key}`"
            size="small"
            @update:model-value="(on: string | number | boolean) => setWrite(system.key, on === true)"
          />
          <span>{{ t('infra.allowWrite') }}</span>
        </div>
      </div>
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
/* 卡片等宽等高：同一行靠 stretch 拉齐，各行靠 grid-auto-rows 拉齐；说明最多三行，完整内容看悬停提示 */
.grants-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); grid-auto-rows: 1fr; gap: 12px; }
.grant-cell { display: flex; flex-direction: column; min-width: 0; overflow: hidden; border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); transition: border-color .2s, background-color .2s; }
.grant-cell:hover { border-color: var(--el-color-primary-light-5); }
.grant-cell.is-checked { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.grant-option.el-checkbox { display: flex; flex: 1; align-items: flex-start; height: auto; margin: 0; padding: 14px 16px; }
.grant-option :deep(.el-checkbox__input) { margin-top: 3px; }
.grant-option :deep(.el-checkbox__label) { display: flex; flex-direction: column; gap: 6px; min-width: 0; padding-left: 10px; white-space: normal; }
.grant-name { display: flex; align-items: center; gap: 8px; min-width: 0; color: var(--el-text-color-primary); font-weight: 500; }
.grant-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grant-name .el-tag { flex-shrink: 0; }
.grant-desc { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 3; color: var(--el-text-color-secondary); font-size: 12px; font-weight: 400; line-height: 1.6; overflow-wrap: anywhere; }
.grant-write { display: flex; align-items: center; gap: 8px; padding: 8px 16px 8px 40px; border-top: 1px solid var(--el-border-color-lighter); color: var(--el-text-color-secondary); font-size: 12px; }
@media (max-width: 767px) { .grants-head { flex-direction: column; gap: 12px; } }
</style>
