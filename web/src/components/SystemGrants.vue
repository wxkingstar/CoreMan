<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { systems, type BusinessSystem } from '@/api/infrastructure'
const props = defineProps<{ botId: string }>()
const emit = defineEmits<{ saved: [] }>()
const { t } = useI18n()
const visible = ref(false), busy = ref(false), version = ref(0)
const keys = ref<string[]>([]), options = ref<BusinessSystem[]>([])
async function open() {
  if (busy.value) return
  busy.value = true
  try {
    const grants = await systems.grants(props.botId)
    keys.value = grants.system_keys; version.value = grants.version
    const all: BusinessSystem[] = []
    for (let page = 1; ; page++) { const data = await systems.list(page); all.push(...data.items); if (all.length >= data.total || !data.items.length) break }
    options.value = all.filter(s => s.enabled && (s.allowed_bot_ids === null || s.allowed_bot_ids.includes(props.botId)))
    visible.value = true
  } catch (e) { ElMessage.error(e instanceof Error ? e.message : String(e)) } finally { busy.value = false }
}
async function save() {
  if (busy.value) return
  busy.value = true
  try { await systems.saveGrants(props.botId, keys.value, version.value); visible.value = false; emit('saved'); ElMessage.success(t('common.saved')) }
  catch (e) { ElMessage.error(e instanceof Error ? e.message : String(e)) } finally { busy.value = false }
}
</script>
<template>
  <el-button
    :loading="busy"
    @click="open"
  >
    {{ t('infra.systemGrants') }}
  </el-button>
  <el-dialog
    v-model="visible"
    :close-on-click-modal="false"
    :title="t('infra.systemGrants')"
    width="560px"
  >
    <p>{{ t('infra.grantsHint') }}</p>
    <el-checkbox-group v-model="keys">
      <div
        v-for="system in options"
        :key="system.key"
      >
        <el-checkbox :value="system.key">
          {{ system.name }}{{ system.default_for_all_bots ? ' · ' + t('infra.defaultAccess') : '' }}
        </el-checkbox>
      </div>
    </el-checkbox-group>
    <template #footer>
      <el-button @click="visible = false">
        {{ t('common.cancel') }}
      </el-button><el-button
        :loading="busy"
        type="primary"
        @click="save"
      >
        {{ t('common.save') }}
      </el-button>
    </template>
  </el-dialog>
</template>
