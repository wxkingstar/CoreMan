<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { bots } from '@/api/admin'
import type { BotMemberOut, BotPermissions } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{ botId: string; createdBy: string | null; perms: BotPermissions | null }>()
/** 协作者变动后通知详情页刷新 member_count。 */
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const membersVisible = ref(false)
const busy = ref(false)
const members = ref<BotMemberOut[]>([])
const memberPick = ref<string[]>([])
// 协作者选择里排除创建者（后端 409）与已有成员（后端 409）。
const memberExclude = computed(() => [props.createdBy, ...members.value.map((m) => m.user_id)].filter((x): x is string => !!x))

function fail(e: unknown): void {
  ElMessage.error(errorMessage(e))
}

async function loadMembers(): Promise<void> {
  try {
    members.value = await bots.members(props.botId)
  } catch (e) {
    fail(e)
  }
}

async function open(): Promise<void> {
  membersVisible.value = true
  memberPick.value = []
  await loadMembers()
}

async function addMembers(): Promise<void> {
  if (!memberPick.value.length) return
  busy.value = true
  try {
    // 逐个加：后端一次只收一个 user_id，且单个失败（已停用 422 / 已是协作者 409）不该拖垮其余。
    for (const uid of memberPick.value) {
      try {
        await bots.addMember(props.botId, uid)
      } catch (e) {
        fail(e)
      }
    }
    memberPick.value = []
    await loadMembers()
    emit('changed')
  } finally {
    busy.value = false
  }
}

async function removeMember(userId: string): Promise<void> {
  busy.value = true
  try {
    await bots.removeMember(props.botId, userId)
    await loadMembers()
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
    v-model="membersVisible"
    :title="t('bots.detail.members')"
    width="640px"
  >
    <p class="muted">
      {{ t('bots.detail.membersHint') }}
    </p>
    <table class="kv members">
      <thead>
        <tr>
          <th>{{ t('users.name') }}</th>
          <th>{{ t('bots.detail.addedAt') }}</th>
          <th v-if="perms?.can_manage_members">
            {{ t('common.actions') }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="m in members"
          :key="m.user_id"
        >
          <td>
            {{ m.display_name }}
            <span class="muted">{{ m.login_name ?? '—' }}</span>
          </td>
          <td>{{ formatDateTime(m.added_at) }}</td>
          <td v-if="perms?.can_manage_members">
            <el-button
              size="small"
              type="danger"
              plain
              :loading="busy"
              :data-test="'remove-member-' + m.user_id"
              @click="removeMember(m.user_id)"
            >
              {{ t('bots.detail.removeMember') }}
            </el-button>
          </td>
        </tr>
        <tr v-if="!members.length">
          <td :colspan="perms?.can_manage_members ? 3 : 2">
            —
          </td>
        </tr>
      </tbody>
    </table>
    <div
      v-if="perms?.can_manage_members"
      class="picker-row"
    >
      <UserPicker
        v-model="memberPick"
        :exclude="memberExclude"
      />
      <el-button
        type="primary"
        :loading="busy"
        :disabled="!memberPick.length"
        data-test="add-member"
        @click="addMembers"
      >
        {{ t('bots.detail.addMember') }}
      </el-button>
    </div>
  </el-dialog>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.kv { border-collapse: collapse; width: 100%; }
.kv th, .kv td { border: 1px solid var(--el-border-color-lighter); padding: 6px 10px; text-align: left; font-weight: normal; word-break: break-all; }
.kv th { background: var(--el-fill-color-light); width: 220px; }
.members th { width: auto; }
.picker-row { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
</style>
