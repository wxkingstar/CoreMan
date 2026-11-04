<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { departments } from '@/api/admin'
import {
  feishuApps,
  type FeishuVisibility,
  type FeishuVisibilityDepartment,
  type FeishuVisibilityPatch,
  type FeishuVisibilityUser,
} from '@/api/feishuApps'
import type { DeptNode } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'
import { useAuthStore } from '@/stores/auth'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ botId: string }>()
/** 应用还没有管理可用范围的权限：请父组件打开扫码补齐（连同这组权限一起申请）。 */
const emit = defineEmits<{ scan: [] }>()
const { t } = useI18n()
const auth = useAuthStore()
const platformAdmin = computed(() => auth.user?.role === 'platform_admin')

const state = ref<FeishuVisibility | null>(null)
const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const restricting = ref(false)
const userIds = ref<string[]>([])
const departmentIds = ref<string[]>([])
const tree = ref<DeptNode[]>([])

const granted = computed(() => (state.value?.permission === 'granted' ? state.value : null))
const showPicker = computed(() => !!granted.value && (!granted.value.visible_to_all || restricting.value))
/** 这个机器人就是平台的可用范围管理应用。 */
const isManager = computed(() => state.value?.manager?.id === props.botId)
const settingManager = ref(false)

async function setManager(botId: string | null): Promise<void> {
  settingManager.value = true
  try {
    const { manager } = await feishuApps.setVisibilityManager(botId)
    if (state.value) state.value = { ...state.value, manager }
    ElMessage.success(t(botId ? 'feishuApp.visibilityManagerSet' : 'feishuApp.visibilityManagerCleared'))
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    settingManager.value = false
  }
}

const hasInvisible = computed(() => {
  const inv = granted.value?.invisible
  return !!inv && (inv.users.length > 0 || inv.departments.length > 0 || inv.groups.length > 0)
})

function userLabel(u: FeishuVisibilityUser): string {
  if (!u.display_name) return t('feishuApp.visibilityUnknownUser', { id: u.feishu_id })
  return u.login_name ? `${u.display_name} (${u.login_name})` : u.display_name
}

function deptLabel(d: FeishuVisibilityDepartment): string {
  return d.name ?? t('feishuApp.visibilityUnknownDept', { id: d.feishu_id })
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    state.value = await feishuApps.visibility(props.botId)
  } catch (e) {
    loadError.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

async function patch(body: FeishuVisibilityPatch): Promise<boolean> {
  saving.value = true
  try {
    state.value = await feishuApps.updateVisibility(props.botId, body)
    ElMessage.success(t('feishuApp.visibilitySaved'))
    return true
  } catch (e) {
    ElMessage.error(errorMessage(e))
    return false
  } finally {
    saving.value = false
  }
}

async function add(): Promise<void> {
  if (!userIds.value.length && !departmentIds.value.length) {
    ElMessage.error(t('feishuApp.visibilityRequired'))
    return
  }
  const body: FeishuVisibilityPatch = { add_user_ids: userIds.value, add_department_ids: departmentIds.value }
  if (granted.value?.visible_to_all) body.visible_to_all = false
  if (await patch(body)) {
    userIds.value = []
    departmentIds.value = []
    restricting.value = false
  }
}

async function confirmThen(message: string, body: FeishuVisibilityPatch): Promise<void> {
  try {
    await ElMessageBox.confirm(message, t('common.confirm'), { type: 'warning' })
  } catch {
    return
  }
  await patch(body)
}

function removeUser(u: FeishuVisibilityUser): void {
  if (u.id) void confirmThen(t('feishuApp.visibilityRemoveConfirm', { name: userLabel(u) }), { remove_user_ids: [u.id] })
}

function removeDept(d: FeishuVisibilityDepartment): void {
  if (d.id) void confirmThen(t('feishuApp.visibilityRemoveConfirm', { name: deptLabel(d) }), { remove_department_ids: [d.id] })
}

function openToAll(): void {
  void confirmThen(t('feishuApp.visibilityAllConfirm'), { visible_to_all: true })
}

onMounted(async () => {
  await load()
  try {
    tree.value = await departments.tree('feishu')
  } catch {
    tree.value = [] // 没同步飞书通讯录时只按成员选择
  }
})
</script>

<template>
  <section
    v-loading="loading"
    class="cm-panel"
    data-test="feishu-app-visibility"
  >
    <h3>{{ t('feishuApp.visibility') }}</h3>
    <p class="cm-page-intro">
      {{ t('feishuApp.visibilityHint') }}
    </p>
    <el-alert
      v-if="loadError"
      type="error"
      :title="loadError"
      :closable="false"
      show-icon
    />
    <template v-else-if="state && state.permission !== 'granted'">
      <p
        v-if="state.permission === 'missing'"
        class="muted"
      >
        {{ t('feishuApp.visibilityManagerHint') }}
      </p>
      <el-alert
        :type="state.permission === 'pending' ? 'info' : 'warning'"
        :title="t(state.permission === 'pending' ? 'feishuApp.visibilityPending' : 'feishuApp.visibilityMissing')"
        :closable="false"
        show-icon
      />
      <el-button
        v-if="state.permission === 'missing'"
        type="primary"
        class="scan"
        data-test="visibility-scan"
        @click="emit('scan')"
      >
        {{ t('feishuApp.visibilityScan') }}
      </el-button>
    </template>
    <template v-else-if="granted">
      <div
        v-if="granted.via === 'manager' || isManager || platformAdmin"
        class="row source"
        data-test="visibility-source"
      >
        <span
          v-if="granted.via === 'manager' && granted.manager"
          class="muted"
        >{{ t('feishuApp.visibilityManagedBy', { name: granted.manager.name }) }}</span>
        <template v-else-if="isManager">
          <el-tag type="success">
            {{ t('feishuApp.visibilityManagerTag') }}
          </el-tag>
          <el-button
            v-if="platformAdmin"
            size="small"
            :loading="settingManager"
            data-test="visibility-manager-clear"
            @click="setManager(null)"
          >
            {{ t('feishuApp.visibilityManagerClear') }}
          </el-button>
        </template>
        <el-button
          v-else-if="platformAdmin"
          size="small"
          :loading="settingManager"
          data-test="visibility-manager-set"
          @click="setManager(botId)"
        >
          {{ t('feishuApp.visibilityManagerSetButton') }}
        </el-button>
      </div>
      <div
        class="row"
        data-test="visibility-current"
      >
        <span class="label">{{ t('feishuApp.visibilityScope') }}</span>
        <span>{{ granted.visible_to_all ? t('feishuApp.visibilityAll') : t('feishuApp.visibilitySome') }}</span>
        <el-button
          v-if="granted.visible_to_all && !restricting"
          size="small"
          :disabled="saving"
          data-test="visibility-restrict"
          @click="restricting = true"
        >
          {{ t('feishuApp.visibilityRestrict') }}
        </el-button>
        <el-button
          v-if="!granted.visible_to_all"
          size="small"
          :loading="saving"
          data-test="visibility-open-all"
          @click="openToAll"
        >
          {{ t('feishuApp.visibilityOpenAll') }}
        </el-button>
      </div>
      <template v-if="!granted.visible_to_all">
        <div class="row">
          <span class="label">{{ t('feishuApp.visibilityUsers') }}</span>
          <div class="tags">
            <el-tag
              v-for="u in granted.users"
              :key="u.feishu_id"
              :closable="!!u.id && !saving"
              :type="u.id ? undefined : 'info'"
              data-test="visibility-user"
              @close="removeUser(u)"
            >
              {{ userLabel(u) }}
            </el-tag>
            <span
              v-if="!granted.users.length"
              class="muted"
            >-</span>
          </div>
        </div>
        <div class="row">
          <span class="label">{{ t('feishuApp.visibilityDepartments') }}</span>
          <div class="tags">
            <el-tag
              v-for="d in granted.departments"
              :key="d.feishu_id"
              :closable="!!d.id && !saving"
              :type="d.id ? undefined : 'info'"
              data-test="visibility-dept"
              @close="removeDept(d)"
            >
              {{ deptLabel(d) }}
            </el-tag>
            <span
              v-if="!granted.departments.length"
              class="muted"
            >-</span>
          </div>
        </div>
        <p
          v-if="granted.groups.length"
          class="muted"
        >
          {{ t('feishuApp.visibilityGroups', { n: granted.groups.length }) }}
        </p>
      </template>
      <div
        v-if="hasInvisible"
        class="row"
      >
        <span class="label">{{ t('feishuApp.visibilityInvisible') }}</span>
        <div class="tags">
          <el-tag
            v-for="u in granted.invisible.users"
            :key="'u' + u.feishu_id"
            type="danger"
          >
            {{ userLabel(u) }}
          </el-tag>
          <el-tag
            v-for="d in granted.invisible.departments"
            :key="'d' + d.feishu_id"
            type="danger"
          >
            {{ deptLabel(d) }}
          </el-tag>
          <span
            v-if="granted.invisible.groups.length"
            class="muted"
          >{{ t('feishuApp.visibilityGroups', { n: granted.invisible.groups.length }) }}</span>
        </div>
      </div>
      <p class="muted">
        {{ t('feishuApp.visibilityNote') }}
      </p>
      <el-form
        v-if="showPicker"
        label-width="120px"
        class="add-form"
        @submit.prevent
      >
        <el-form-item :label="t('feishuApp.visibilityAddUsers')">
          <UserPicker v-model="userIds" />
        </el-form-item>
        <el-form-item :label="t('feishuApp.visibilityAddDepartments')">
          <el-tree-select
            v-model="departmentIds"
            :data="tree"
            :props="{ label: 'name', children: 'children' }"
            node-key="id"
            multiple
            show-checkbox
            check-strictly
            filterable
            style="width: 100%"
            :placeholder="t('feishuApp.visibilityDepartments')"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="saving"
            data-test="visibility-add"
            @click="add"
          >
            {{ t('feishuApp.visibilityAdd') }}
          </el-button>
          <el-button
            v-if="restricting"
            :disabled="saving"
            @click="restricting = false"
          >
            {{ t('common.cancel') }}
          </el-button>
        </el-form-item>
      </el-form>
    </template>
  </section>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.row { display: flex; gap: 12px; align-items: flex-start; margin-bottom: 10px; flex-wrap: wrap; }
.label { font-weight: 600; min-width: 72px; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; flex: 1; }
.scan { margin-top: 12px; }
.source { align-items: center; }
.add-form { margin-top: 12px; max-width: 640px; }
</style>
