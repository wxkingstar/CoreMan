<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { teams as teamsApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { RuleIn, TeamIn, TeamOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ teams?: TeamOut[] }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const auth = useAuthStore()

const canManage = computed(() => auth.user?.role === 'ai_committee' || auth.user?.role === 'platform_admin')
const rows = ref<TeamOut[]>(props.teams ?? [])

// Parent (UsersView) passes `:teams="teamList"`, which starts as `[]` and is only
// filled later in the parent's own onMounted — so a one-time snapshot at setup would
// leave this table permanently empty. Stay in sync with the prop whenever it's provided.
watch(
  () => props.teams,
  (v) => {
    if (v) rows.value = v
  },
  { immediate: true },
)

async function load() {
  rows.value = await teamsApi.list()
}

onMounted(async () => {
  if (!props.teams) await load()
})

function emptyForm(): TeamIn {
  return { slug: '', name_zh: '', name_ja: null, name_en: null, sort_order: 0, enabled: true }
}

const dialogVisible = ref(false)
const dialogEditingId = ref<string | null>(null)
const formRef = ref<FormInstance>()
const form = reactive<TeamIn>(emptyForm())
const formRules = computed<FormRules>(() => ({
  slug: [
    {
      required: true,
      pattern: /^[a-z0-9][a-z0-9_-]{1,49}$/,
      message: t('login.required', { field: t('teams.slug') }),
      trigger: 'blur',
    },
  ],
  name_zh: [{ required: true, message: t('login.required', { field: t('teams.nameZh') }), trigger: 'blur' }],
}))

function openCreate() {
  dialogEditingId.value = null
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}

function openEdit(team: TeamOut) {
  dialogEditingId.value = team.id
  Object.assign(form, {
    slug: team.slug,
    name_zh: team.name_zh,
    name_ja: team.name_ja,
    name_en: team.name_en,
    sort_order: team.sort_order,
    enabled: team.enabled,
  })
  dialogVisible.value = true
}

const saving = ref(false)

/** 提交防重入：双击第二次会撞唯一约束 409。 */
async function submitForm() {
  if (saving.value) return
  saving.value = true
  try {
    await saveTeam()
  } finally {
    saving.value = false
  }
}

async function saveTeam() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  try {
    if (dialogEditingId.value) await teamsApi.update(dialogEditingId.value, { ...form })
    else await teamsApi.create({ ...form })
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    await load()
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}

async function askDelete(team: TeamOut) {
  try {
    await ElMessageBox.confirm(t('teams.deleteConfirm'), t('common.delete'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    await teamsApi.remove(team.id)
    ElMessage.success(t('common.deleted'))
    await load()
    emit('changed')
  } catch (e) {
    // The backend rejects with 409 when the team still has members; surface its message.
    ElMessage.error(e instanceof ApiError ? e.message : String(e))
  }
}

const rulesVisible = ref(false)
const rulesTeamId = ref<string | null>(null)
const ruleRows = ref<RuleIn[]>([])

function openRules(team: TeamOut) {
  rulesTeamId.value = team.id
  ruleRows.value = team.rules.map((r) => ({
    platform: r.platform,
    dept_path_contains: r.dept_path_contains,
    sort_order: r.sort_order,
  }))
  rulesVisible.value = true
}

function addRuleRow() {
  ruleRows.value.push({ platform: null, dept_path_contains: '', sort_order: 0 })
}

function removeRuleRow(index: number) {
  ruleRows.value.splice(index, 1)
}

function setRulePlatform(row: RuleIn, value: string | number | boolean | undefined) {
  row.platform = value === 'wecom' || value === 'feishu' ? value : null
}

const savingRules = ref(false)

async function saveRules() {
  if (!rulesTeamId.value || savingRules.value) return
  savingRules.value = true
  try {
    await teamsApi.replaceRules(rulesTeamId.value, ruleRows.value)
    ElMessage.success(t('common.saved'))
    rulesVisible.value = false
    await load()
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    savingRules.value = false
  }
}
</script>

<template>
  <div class="teams-panel">
    <div class="toolbar">
      <el-button
        v-if="canManage"
        type="primary"
        data-test="create-team"
        @click="openCreate"
      >
        {{ t('common.create') }}
      </el-button>
    </div>

    <el-table :data="rows">
      <el-table-column
        min-width="140"
        prop="slug"
        :label="t('teams.slug')"
      />
      <el-table-column
        min-width="140"
        prop="name_zh"
        :label="t('teams.nameZh')"
      />
      <el-table-column
        min-width="140"
        prop="name_ja"
        :label="t('teams.nameJa')"
      />
      <el-table-column
        min-width="140"
        prop="name_en"
        :label="t('teams.nameEn')"
      />
      <el-table-column
        min-width="140"
        prop="sort_order"
        :label="t('teams.sortOrder')"
      />
      <el-table-column
        min-width="140"
        :label="t('common.enabled')"
      >
        <template #default="{ row }: { row: TeamOut }">
          <el-tag :type="row.enabled ? 'success' : 'info'">
            {{ row.enabled ? t('common.enabled') : t('common.disabled') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        prop="member_count"
        :label="t('teams.memberCount')"
      />
      <el-table-column
        min-width="140"
        :label="t('teams.rules')"
      >
        <template #default="{ row }: { row: TeamOut }">
          {{ row.rules.length }}
        </template>
      </el-table-column>
      <el-table-column
        v-if="canManage"
        min-width="140"
        :label="t('common.actions')"
      >
        <template #default="{ row }: { row: TeamOut }">
          <el-button
            size="small"
            :data-test="'edit-team-' + row.id"
            @click="openEdit(row)"
          >
            {{ t('common.edit') }}
          </el-button>
          <el-button
            size="small"
            :data-test="'rules-' + row.id"
            @click="openRules(row)"
          >
            {{ t('teams.rules') }}
          </el-button>
          <el-button
            size="small"
            type="danger"
            :data-test="'delete-team-' + row.id"
            @click="askDelete(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :close-on-click-modal="false"
      width="560px"
      :title="dialogEditingId ? t('common.edit') : t('common.create')"
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="formRules"
        label-width="90px"
      >
        <el-form-item
          :label="t('teams.slug')"
          prop="slug"
        >
          <el-input v-model="form.slug" />
        </el-form-item>
        <el-form-item
          :label="t('teams.nameZh')"
          prop="name_zh"
        >
          <el-input v-model="form.name_zh" />
        </el-form-item>
        <el-form-item :label="t('teams.nameJa')">
          <el-input
            :model-value="form.name_ja ?? ''"
            @update:model-value="form.name_ja = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('teams.nameEn')">
          <el-input
            :model-value="form.name_en ?? ''"
            @update:model-value="form.name_en = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('teams.sortOrder')">
          <el-input-number
            v-model="form.sort_order"
            :min="0"
          />
        </el-form-item>
        <el-form-item :label="t('common.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-team"
          :loading="saving"
          @click="submitForm"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="rulesVisible"
      width="720px"
      :title="t('teams.rules')"
    >
      <el-table :data="ruleRows">
        <el-table-column
          min-width="140"
          :label="t('teams.platform')"
        >
          <template #default="{ row }: { row: RuleIn }">
            <el-select
              :model-value="row.platform"
              clearable
              :placeholder="t('teams.anyPlatform')"
              @change="(v: string | number | boolean | undefined) => setRulePlatform(row, v)"
            >
              <el-option
                :label="t('platforms.wecom')"
                value="wecom"
              />
              <el-option
                :label="t('platforms.feishu')"
                value="feishu"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column
          min-width="140"
          :label="t('teams.pathContains')"
        >
          <template #default="{ row }: { row: RuleIn }">
            <el-input v-model="row.dept_path_contains" />
          </template>
        </el-table-column>
        <el-table-column
          min-width="140"
          :label="t('teams.sortOrder')"
        >
          <template #default="{ row }: { row: RuleIn }">
            <el-input-number
              v-model="row.sort_order"
              :min="0"
            />
          </template>
        </el-table-column>
        <el-table-column min-width="140">
          <template #default="{ $index }">
            <el-button
              size="small"
              type="danger"
              @click="removeRuleRow($index)"
            >
              {{ t('common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button
        data-test="add-rule"
        @click="addRuleRow"
      >
        {{ t('teams.addRule') }}
      </el-button>
      <template #footer>
        <el-button @click="rulesVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-rules"
          :loading="savingRules"
          @click="saveRules"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar { margin-bottom: 12px; }
</style>
