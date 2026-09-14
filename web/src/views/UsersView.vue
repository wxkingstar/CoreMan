<script setup lang="ts">
import LoadState from '@/components/LoadState.vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { departments, teams as teamsApi, users as usersApi } from '@/api/admin'
import type { DeptNode, Role, TeamOut, UserOut, UserPatch } from '@/api/types'
import DeptTree from '@/components/DeptTree.vue'
import { usePaged } from '@/composables/usePaged'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/format'
import TeamsPanel from '@/views/TeamsPanel.vue'

const { t } = useI18n()
const auth = useAuthStore()
const me = computed(() => auth.user)
const tab = ref<'users' | 'teams'>('users')
const teamList = ref<TeamOut[]>([])
const tree = ref<DeptNode[]>([])
const treeVisible = ref(false)

type UserFilters = { keyword: string; team_id: string | null; role: string | null; status: string | null; unassigned: boolean }
const paged = usePaged<UserOut, UserFilters>(
  (q) => usersApi.list(q),
  { keyword: '', team_id: null, role: null, status: null, unassigned: false },
)

const ALL_ROLES: Role[] = ['platform_admin', 'ai_committee', 'team_lead', 'member']

const ROLE_OPTIONS: Record<Role, Role[]> = {
  platform_admin: ['platform_admin', 'ai_committee', 'team_lead', 'member'],
  ai_committee: ['ai_committee', 'team_lead', 'member'],
  team_lead: ['team_lead', 'member'],
  member: [],
}

function canEdit(u: UserOut): boolean {
  const m = me.value
  if (!m || u.source === 'bootstrap') return false
  if (m.role === 'platform_admin') return true
  if (m.role === 'ai_committee') return u.role !== 'platform_admin'
  if (m.role === 'team_lead') return !!m.team_id && u.team_id === m.team_id && (u.role === 'member' || u.role === 'team_lead')
  return false
}

// 停用/启用只有 ai_committee 与 platform_admin 能做（后端 can_edit_user 同样拒绝 team_lead）
const canEditStatus = computed(() => me.value?.role === 'platform_admin' || me.value?.role === 'ai_committee')

function roleOptions(): Role[] {
  const r = me.value?.role as Role | undefined
  return r ? (ROLE_OPTIONS[r] ?? []) : []
}

function teamOptionsFor(): TeamOut[] {
  if (me.value?.role === 'team_lead') return teamList.value.filter((tm) => tm.id === me.value?.team_id)
  return teamList.value
}

async function patch(u: UserOut, body: UserPatch) {
  try {
    const updated = await usersApi.patch(u.id, body)
    Object.assign(u, updated)
    ElMessage.success(t('common.saved'))
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
    await paged.load()
  }
}

const editing = reactive<{ visible: boolean; user: UserOut | null; form: UserPatch }>({
  visible: false,
  user: null,
  form: {},
})
const editDrawerTitle = computed(() => t('users.edit') + (editing.user ? `：${editing.user.display_name}` : ''))

function openEdit(u: UserOut) {
  editing.user = u
  editing.form = { position: u.position, skills: u.skills, locale: u.locale, status: u.status }
  editing.visible = true
}

/** 只提交与原值不同的字段：未变化的字段会被后端记进 manual_fields，从此不再被同步刷新。 */
async function saveEdit() {
  const u = editing.user
  if (!u) return
  const body: UserPatch = {}
  if (editing.form.position !== u.position) body.position = editing.form.position
  if (editing.form.skills !== u.skills) body.skills = editing.form.skills
  if (editing.form.locale !== u.locale) body.locale = editing.form.locale
  if (canEditStatus.value && editing.form.status !== u.status) body.status = editing.form.status
  if (Object.keys(body).length === 0) {
    ElMessage.success(t('common.saved'))
    editing.visible = false
    return
  }
  await patch(u, body)
  editing.visible = false
}

async function showTree() {
  tree.value = await departments.tree('wecom')
  treeVisible.value = true
}

async function reloadTeams() {
  teamList.value = await teamsApi.list()
}

function search() {
  paged.page.value = 1
  paged.load()
}

function resetFilters() {
  paged.reset()
  paged.load()
}

function onFilterChange(key: 'team_id' | 'role' | 'status', v: string | undefined) {
  paged.filters[key] = v ? v : null
}

function onPageChange(p: number) {
  paged.page.value = p
  paged.load()
}

function onSizeChange(size: number) {
  paged.perPage.value = size
  paged.page.value = 1
  paged.load()
}

onMounted(async () => {
  teamList.value = await teamsApi.list()
  await paged.load()
})
</script>

<template>
  <div class="users-view">
    <div class="page-header cm-page-header">
      <h2>{{ t('users.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.users') }}
      </p>
      <el-button @click="showTree">
        {{ t('users.deptTree') }}
      </el-button>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane
        :label="t('users.tabUsers')"
        name="users"
      >
        <el-form
          :inline="true"
          @submit.prevent
        >
          <el-form-item :label="t('users.keyword')">
            <el-input
              v-model="paged.filters.keyword"
              :placeholder="t('users.keyword')"
              clearable
              style="width: 160px"
              @keyup.enter="search"
            />
          </el-form-item>
          <el-form-item :label="t('users.team')">
            <el-select
              :model-value="paged.filters.team_id"
              clearable
              :placeholder="t('users.team')"
              style="width: 140px"
              @change="(v: string | undefined) => onFilterChange('team_id', v)"
            >
              <el-option
                v-for="tm in teamList"
                :key="tm.id"
                :label="tm.name_zh"
                :value="tm.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('users.role')">
            <el-select
              :model-value="paged.filters.role"
              clearable
              :placeholder="t('users.role')"
              style="width: 140px"
              @change="(v: string | undefined) => onFilterChange('role', v)"
            >
              <el-option
                v-for="r in ALL_ROLES"
                :key="r"
                :label="t('users.roles.' + r)"
                :value="r"
              />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('users.status')">
            <el-select
              :model-value="paged.filters.status"
              clearable
              :placeholder="t('users.status')"
              style="width: 120px"
              @change="(v: string | undefined) => onFilterChange('status', v)"
            >
              <el-option
                :label="t('users.statuses.active')"
                value="active"
              />
              <el-option
                :label="t('users.statuses.disabled')"
                value="disabled"
              />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('users.unassigned')">
            <el-switch v-model="paged.filters.unassigned" />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              @click="search"
            >
              {{ t('common.search') }}
            </el-button>
            <el-button @click="resetFilters">
              {{ t('common.reset') }}
            </el-button>
          </el-form-item>
        </el-form>

        <LoadState
          :error="paged.error.value"
          @retry="paged.load"
        />
        <el-table
          v-loading="paged.loading.value"
          :data="paged.items.value"
        >
          <el-table-column
            min-width="140"
            :label="t('users.name')"
          >
            <template #default="{ row }: { row: UserOut }">
              <div class="name-cell">
                <el-avatar
                  :size="24"
                  :src="row.avatar_url ?? undefined"
                >
                  {{ row.display_name.charAt(0) }}
                </el-avatar>
                <div>
                  <div>
                    {{ row.display_name }}
                    <el-tag
                      size="small"
                      type="info"
                    >
                      {{ row.source ? t('users.sources.' + row.source) : '' }}
                    </el-tag>
                  </div>
                  <div class="muted">
                    {{ row.login_name }}
                  </div>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            prop="email"
            :label="t('users.email')"
          />
          <el-table-column
            min-width="140"
            :label="t('users.departments')"
          >
            <template #default="{ row }: { row: UserOut }">
              <span>{{ row.departments[0] ?? '-' }}</span>
              <el-tooltip
                v-if="row.departments.length > 1"
                :content="row.departments.slice(1).join('; ')"
              >
                <span class="dept-more">+{{ row.departments.length - 1 }}</span>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('users.team')"
          >
            <template #default="{ row }: { row: UserOut }">
              <el-select
                v-if="canEdit(row)"
                :data-test="'team-select-' + row.id"
                :model-value="row.team_id"
                clearable
                :placeholder="t('users.noTeam')"
                style="width: 130px"
                @change="(v: string | undefined) => patch(row, { team_id: v || null })"
              >
                <el-option
                  v-for="tm in teamOptionsFor()"
                  :key="tm.id"
                  :label="tm.name_zh"
                  :value="tm.id"
                />
              </el-select>
              <span v-else>{{ row.team_name ?? t('users.noTeam') }}</span>
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('users.role')"
          >
            <template #default="{ row }: { row: UserOut }">
              <el-select
                v-if="canEdit(row)"
                :data-test="'role-select-' + row.id"
                :model-value="row.role"
                :disabled="row.id === me?.id"
                style="width: 130px"
                @change="(v: Role) => patch(row, { role: v })"
              >
                <el-option
                  v-for="r in roleOptions()"
                  :key="r"
                  :label="t('users.roles.' + r)"
                  :value="r"
                />
              </el-select>
              <span v-else>{{ row.role ? t('users.roles.' + row.role) : '' }}</span>
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('users.botAccessible')"
          >
            <template #default="{ row }: { row: UserOut }">
              <el-switch
                :model-value="row.bot_accessible"
                :disabled="!canEdit(row)"
                @change="(v: boolean) => patch(row, { bot_accessible: v })"
              />
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('users.status')"
          >
            <template #default="{ row }: { row: UserOut }">
              <el-tag :type="row.status === 'active' ? 'success' : 'info'">
                {{ row.status ? t('users.statuses.' + row.status) : '' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('users.lastLogin')"
          >
            <template #default="{ row }: { row: UserOut }">
              {{ formatDateTime(row.last_login_at) }}
            </template>
          </el-table-column>
          <el-table-column
            min-width="140"
            :label="t('common.actions')"
          >
            <template #default="{ row }: { row: UserOut }">
              <el-button
                v-if="canEdit(row)"
                size="small"
                :data-test="'edit-' + row.id"
                @click="openEdit(row)"
              >
                {{ t('users.edit') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-pagination
          :total="paged.total.value"
          :current-page="paged.page.value"
          :page-size="paged.perPage.value"
          :page-sizes="[20, 50, 100, 200]"
          layout="total, prev, pager, next, sizes"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </el-tab-pane>

      <el-tab-pane
        :label="t('users.tabTeams')"
        name="teams"
      >
        <TeamsPanel
          :teams="teamList"
          @changed="reloadTeams"
        />
      </el-tab-pane>
    </el-tabs>

    <el-drawer
      v-model="editing.visible"
      size="560px"
      :close-on-click-modal="false"
      :title="editDrawerTitle"
    >
      <el-form label-width="80px">
        <el-form-item
          :label="t('users.position')"
          data-test="edit-position"
        >
          <el-input
            :model-value="editing.form.position ?? ''"
            @update:model-value="editing.form.position = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('users.skills')">
          <el-input
            type="textarea"
            :rows="4"
            :model-value="editing.form.skills ?? ''"
            @update:model-value="editing.form.skills = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('users.locale')">
          <el-select v-model="editing.form.locale">
            <el-option
              label="中文"
              value="zh"
            />
            <el-option
              label="日本語"
              value="ja"
            />
            <el-option
              label="English"
              value="en"
            />
          </el-select>
        </el-form-item>
        <el-form-item
          v-if="canEditStatus"
          :label="t('users.status')"
          data-test="edit-status"
        >
          <el-select
            v-model="editing.form.status"
            :disabled="editing.user?.id === me?.id"
          >
            <el-option
              :label="t('users.statuses.active')"
              value="active"
            />
            <el-option
              :label="t('users.statuses.disabled')"
              value="disabled"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editing.visible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-edit"
          @click="saveEdit"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-drawer>

    <el-drawer
      v-model="treeVisible"
      size="560px"
      :title="t('users.deptTree')"
    >
      <DeptTree :nodes="tree" />
    </el-drawer>
  </div>
</template>

<style scoped>
.name-cell { display: flex; align-items: center; gap: 8px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.dept-more { margin-left: 4px; color: var(--el-text-color-secondary); cursor: default; }
</style>
