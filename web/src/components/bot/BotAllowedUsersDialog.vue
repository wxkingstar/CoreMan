<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots } from '@/api/admin'
import type { AllowedUserOut, BotPermissions } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'

const props = defineProps<{ botId: string; perms: BotPermissions | null }>()
/** 白名单保存后通知详情页刷新 allowed_user_count。 */
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const allowedVisible = ref(false)
const busy = ref(false)
const allowed = ref<AllowedUserOut[]>([])
const allowedIds = ref<string[]>([])
const allowedInitial = computed(() => allowed.value.map((u) => ({ id: u.user_id, display_name: u.display_name, login_name: u.login_name })))

function fail(e: unknown): void {
  ElMessage.error(errorMessage(e))
}

async function loadAllowed(): Promise<void> {
  try {
    allowed.value = await bots.allowedUsers(props.botId)
    allowedIds.value = allowed.value.map((u) => u.user_id)
  } catch (e) {
    fail(e)
  }
}

async function open(): Promise<void> {
  allowedVisible.value = true
  await loadAllowed()
}

async function saveAllowed(): Promise<void> {
  busy.value = true
  try {
    allowed.value = await bots.setAllowedUsers(props.botId, allowedIds.value)
    allowedIds.value = allowed.value.map((u) => u.user_id)
    ElMessage.success(t('common.saved'))
    emit('changed')
  } catch (e) {
    fail(e)
  } finally {
    busy.value = false
  }
}

defineExpose({ open })
</script>

<template>
  <el-dialog
    v-model="allowedVisible"
    :title="t('bots.detail.allowedUsers')"
    width="640px"
  >
    <p class="muted">
      {{ t('bots.detail.allowedHint') }}
    </p>
    <template v-if="perms?.can_edit">
      <div class="picker-row">
        <UserPicker
          v-model="allowedIds"
          :initial="allowedInitial"
        />
        <el-button
          type="primary"
          :loading="busy"
          data-test="save-allowed"
          @click="saveAllowed"
        >
          {{ t('bots.detail.save') }}
        </el-button>
      </div>
    </template>
    <ul v-else>
      <li
        v-for="u in allowed"
        :key="u.user_id"
      >
        {{ u.display_name }}<span class="muted">{{ u.login_name ? ` (${u.login_name})` : '' }}</span>
      </li>
      <li v-if="!allowed.length">
        —
      </li>
    </ul>
  </el-dialog>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.picker-row { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
</style>
