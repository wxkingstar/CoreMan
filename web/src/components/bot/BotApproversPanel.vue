<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots } from '@/api/admin'
import type { AllowedUserOut, BotPermissions } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'

const props = defineProps<{ botId: string; perms: BotPermissions | null }>()
const { t } = useI18n()
const busy = ref(false)
const approvers = ref<AllowedUserOut[]>([])
const approverIds = ref<string[]>([])
const approverInitial = computed(() => approvers.value.map((u) => ({ id: u.user_id, display_name: u.display_name, login_name: u.login_name })))

function fail(e: unknown): void {
  ElMessage.error(errorMessage(e))
}

async function loadApprovers(): Promise<void> {
  try {
    approvers.value = await bots.approvers(props.botId)
    approverIds.value = approvers.value.map((u) => u.user_id)
  } catch (e) {
    fail(e)
  }
}

async function saveApprovers(): Promise<void> {
  busy.value = true
  try {
    approvers.value = await bots.setApprovers(props.botId, approverIds.value)
    approverIds.value = approvers.value.map((u) => u.user_id)
    ElMessage.success(t('common.saved'))
  } catch (e) {
    fail(e)
  } finally {
    busy.value = false
  }
}

onMounted(loadApprovers)
</script>

<template>
  <section class="cm-panel">
    <h3>{{ t('bots.detail.approvers') }}<span class="count">（{{ approvers.length }}）</span></h3>
    <p class="hint">
      {{ t('bots.detail.approversHint') }}
    </p>
    <template v-if="perms?.can_edit">
      <div class="picker-row">
        <UserPicker
          v-model="approverIds"
          :initial="approverInitial"
        />
        <el-button
          type="primary"
          :loading="busy"
          data-test="save-approvers"
          @click="saveApprovers"
        >
          {{ t('bots.detail.save') }}
        </el-button>
      </div>
    </template>
    <ul v-else>
      <li
        v-for="u in approvers"
        :key="u.user_id"
      >
        {{ u.display_name }}<span class="muted">{{ u.login_name ? ` (${u.login_name})` : '' }}</span>
      </li>
      <li v-if="!approvers.length">
        —
      </li>
    </ul>
  </section>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
h3 { margin: 0 0 6px; }
.count { color: var(--el-text-color-secondary); font-weight: 400; }
.hint { margin: 0 0 16px; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.7; }
ul { margin: 0; padding-left: 20px; }
.picker-row { display: flex; align-items: center; gap: 8px; }
.picker-row > :first-child { flex: 1; min-width: 0; }
</style>
