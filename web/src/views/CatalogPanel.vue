<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { catalog } from '@/api/admin'
import type { CatalogIn, CatalogOut, CatalogPatch, CatalogReplacePlan } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const { t } = useI18n()
const auth = useAuthStore()
const canManage = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))

const rows = ref<CatalogOut[]>([])
const loading = ref(false)

// 后端已按 (provider, -sort_order, model) 排好序，这里只按 provider 切段，不再重排。
const groups = computed<{ provider: string; rows: CatalogOut[] }[]>(() => {
  const out: { provider: string; rows: CatalogOut[] }[] = []
  for (const row of rows.value) {
    const last = out[out.length - 1]
    if (last && last.provider === row.provider) last.rows.push(row)
    else out.push({ provider: row.provider, rows: [row] })
  }
  return out
})

async function load() {
  loading.value = true
  try {
    rows.value = await catalog.list()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    loading.value = false
  }
}

async function patch(row: CatalogOut, body: CatalogPatch, reload = false) {
  try {
    Object.assign(row, await catalog.patch(row.provider, row.model, body))
    ElMessage.success(t('common.saved'))
    // 退役 / 改默认会连带改动同 provider 下的别的行（后端会改派默认），得整表重取。
    if (reload) await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
    await load()
  }
}

function onRetired(row: CatalogOut, value: boolean | string | number) {
  patch(row, { retired: Boolean(value) }, true)
}

function onXhigh(row: CatalogOut, value: boolean | string | number) {
  patch(row, { supports_xhigh: Boolean(value) })
}

function onMax(row: CatalogOut, value: boolean | string | number) {
  patch(row, { supports_max: Boolean(value) })
}

function onSortOrder(row: CatalogOut, value: number | undefined) {
  if (value === undefined || value === row.sort_order) return
  patch(row, { sort_order: value })
}

function onDefault(row: CatalogOut) {
  if (row.is_default) return
  patch(row, { is_default: true }, true)
}

async function remove(row: CatalogOut) {
  try {
    await ElMessageBox.confirm(t('catalog.deleteConfirm'), t('common.delete'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    await catalog.remove(row.provider, row.model)
    ElMessage.success(t('common.deleted'))
    await load()
  } catch (e) {
    // 409「仍有机器人使用该模型」直接把后端的话给用户看。
    ElMessage.error(errorMessage(e))
  }
}

function emptyForm(): CatalogIn {
  return { provider: '', model: '', display_name: null, is_default: false, retired: false, supports_xhigh: false, supports_max: false, sort_order: 0 }
}

const dialogVisible = ref(false)
const form = reactive<CatalogIn>(emptyForm())

function openCreate() {
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}

const saving = ref(false)

async function submitForm() {
  if (saving.value) return
  if (!form.provider || !form.model) {
    ElMessage.error(t('login.required', { field: t('catalog.model') }))
    return
  }
  saving.value = true
  try {
    await catalog.create({ ...form })
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}

// 换新版本：先预览（dry_run）看会改哪些 AI 员工与运行时，再确认执行。
const replaceVisible = ref(false)
const replaceFrom = ref<CatalogOut | null>(null)
const replaceForm = reactive({ new: '', old: [] as string[], display_name: '', make_default: false, supports_xhigh: false, supports_max: false })
const replacePlan = ref<CatalogReplacePlan | null>(null)
const replacing = ref(false)
const replaceCandidates = computed(() =>
  rows.value.filter((r) => r.provider === replaceFrom.value?.provider && !r.retired),
)

function openReplace(row: CatalogOut) {
  replaceFrom.value = row
  Object.assign(replaceForm, {
    new: '',
    old: [row.model],
    display_name: '',
    make_default: false,
    supports_xhigh: row.supports_xhigh,
    supports_max: row.supports_max,
  })
  replacePlan.value = null
  replaceVisible.value = true
}

function replaceBody(dryRun: boolean) {
  return {
    provider: replaceFrom.value!.provider,
    new: replaceForm.new.trim(),
    old: replaceForm.old,
    display_name: replaceForm.display_name.trim() || null,
    make_default: replaceForm.make_default,
    supports_xhigh: replaceForm.supports_xhigh,
    supports_max: replaceForm.supports_max,
    dry_run: dryRun,
  }
}

async function submitReplace(dryRun: boolean) {
  if (replacing.value || !replaceFrom.value) return
  if (!replaceForm.new.trim() || !replaceForm.old.length) {
    ElMessage.error(t('login.required', { field: t('catalog.replaceNew') }))
    return
  }
  replacing.value = true
  try {
    const plan = await catalog.replace(replaceBody(dryRun))
    if (dryRun) {
      replacePlan.value = plan
      return
    }
    ElMessage.success(t('catalog.replaceDone', { bots: plan.bots.length, relays: plan.relays.length }))
    replaceVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    replacing.value = false
  }
}

// 改了表单就作废旧预览，确认按钮只对看过的预览生效。
function onReplaceEdit() {
  replacePlan.value = null
}

onMounted(load)
</script>

<template>
  <div
    v-loading="loading"
    class="catalog-panel"
  >
    <div class="panel-header">
      <h3>{{ t('catalog.title') }}</h3>
      <el-button
        v-if="canManage"
        type="primary"
        data-test="create-model"
        @click="openCreate"
      >
        {{ t('catalog.add') }}
      </el-button>
    </div>

    <div
      v-for="group in groups"
      :key="group.provider"
      class="provider-group"
    >
      <h4>{{ group.provider }}</h4>
      <el-table :data="group.rows">
        <el-table-column
          min-width="140"
          prop="model"
          :label="t('catalog.model')"
        />
        <el-table-column
          min-width="140"
          :label="t('catalog.displayName')"
        >
          <template #default="{ row }: { row: CatalogOut }">
            {{ row.display_name ?? '—' }}
          </template>
        </el-table-column>
        <el-table-column
          min-width="140"
          :label="t('catalog.backend')"
        >
          <template #default="{ row }: { row: CatalogOut }">
            <el-tag
              size="small"
              :type="row.backend === 'codex' ? 'warning' : row.backend === 'opencode' ? 'success' : 'primary'"
            >
              {{ row.backend }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          :label="t('catalog.isDefault')"
          width="90"
        >
          <template #default="{ row }: { row: CatalogOut }">
            <el-radio
              :data-test="'default-' + row.model"
              :model-value="row.is_default ? row.model : ''"
              :value="row.model"
              :disabled="!canManage || row.retired"
              @change="onDefault(row)"
            />
          </template>
        </el-table-column>
        <el-table-column
          :label="t('catalog.retired')"
          width="90"
        >
          <template #default="{ row }: { row: CatalogOut }">
            <el-switch
              :data-test="'retired-' + row.model"
              :model-value="row.retired"
              :disabled="!canManage"
              @change="onRetired(row, $event)"
            />
          </template>
        </el-table-column>
        <el-table-column
          :label="t('catalog.xhigh')"
          width="90"
        >
          <template #default="{ row }: { row: CatalogOut }">
            <el-switch
              :data-test="'xhigh-' + row.model"
              :model-value="row.supports_xhigh"
              :disabled="!canManage"
              @change="onXhigh(row, $event)"
            />
          </template>
        </el-table-column>
        <el-table-column
          :label="t('catalog.max')"
          width="90"
        >
          <template #default="{ row }: { row: CatalogOut }">
            <el-switch
              :data-test="'max-' + row.model"
              :model-value="row.supports_max"
              :disabled="!canManage"
              @change="onMax(row, $event)"
            />
          </template>
        </el-table-column>
        <el-table-column
          :label="t('catalog.sortOrder')"
          width="150"
        >
          <template #default="{ row }: { row: CatalogOut }">
            <el-input-number
              :data-test="'sort-' + row.model"
              :model-value="row.sort_order"
              :min="0"
              :max="9999"
              size="small"
              controls-position="right"
              :disabled="!canManage"
              @change="onSortOrder(row, $event)"
            />
          </template>
        </el-table-column>
        <el-table-column
          v-if="canManage"
          :label="t('common.actions')"
          width="190"
        >
          <template #default="{ row }: { row: CatalogOut }">
            <el-button
              v-if="!row.retired"
              size="small"
              :data-test="'replace-model-' + row.model"
              @click="openReplace(row)"
            >
              {{ t('catalog.replace') }}
            </el-button>
            <el-button
              size="small"
              type="danger"
              :data-test="'delete-model-' + row.model"
              @click="remove(row)"
            >
              {{ t('common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :close-on-click-modal="false"
      :title="t('catalog.add')"
      width="520px"
      destroy-on-close
    >
      <el-form
        :model="form"
        label-width="120px"
      >
        <el-form-item :label="t('relays.provider')">
          <el-input v-model="form.provider" />
        </el-form-item>
        <el-form-item :label="t('catalog.model')">
          <el-input v-model="form.model" />
        </el-form-item>
        <el-form-item :label="t('catalog.displayName')">
          <el-input
            :model-value="form.display_name ?? ''"
            @update:model-value="form.display_name = ($event as string) || null"
          />
        </el-form-item>
        <el-form-item :label="t('catalog.isDefault')">
          <el-switch v-model="form.is_default" />
        </el-form-item>
        <el-form-item :label="t('catalog.xhigh')">
          <el-switch v-model="form.supports_xhigh" />
        </el-form-item>
        <el-form-item :label="t('catalog.max')">
          <el-switch v-model="form.supports_max" />
        </el-form-item>
        <el-form-item :label="t('catalog.sortOrder')">
          <el-input-number
            v-model="form.sort_order"
            :min="0"
            :max="9999"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          data-test="save-model"
          :loading="saving"
          @click="submitForm"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
    <el-dialog
      v-model="replaceVisible"
      :close-on-click-modal="false"
      :title="t('catalog.replaceTitle', { model: replaceFrom?.model ?? '' })"
      width="560px"
      destroy-on-close
    >
      <p class="replace-hint">
        {{ t('catalog.replaceHint') }}
      </p>
      <el-form
        :model="replaceForm"
        label-width="120px"
      >
        <el-form-item :label="t('catalog.replaceOld')">
          <el-select
            v-model="replaceForm.old"
            multiple
            data-test="replace-old"
            @change="onReplaceEdit"
          >
            <el-option
              v-for="r in replaceCandidates"
              :key="r.model"
              :label="r.model"
              :value="r.model"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('catalog.replaceNew')">
          <el-input
            v-model="replaceForm.new"
            data-test="replace-new"
            @input="onReplaceEdit"
          />
        </el-form-item>
        <el-form-item :label="t('catalog.displayName')">
          <el-input
            v-model="replaceForm.display_name"
            @input="onReplaceEdit"
          />
        </el-form-item>
        <el-form-item :label="t('catalog.replaceDefault')">
          <el-switch
            v-model="replaceForm.make_default"
            @change="onReplaceEdit"
          />
          <span class="replace-note">{{ t('catalog.replaceDefaultHint') }}</span>
        </el-form-item>
        <el-form-item :label="t('catalog.xhigh')">
          <el-switch
            v-model="replaceForm.supports_xhigh"
            @change="onReplaceEdit"
          />
        </el-form-item>
        <el-form-item :label="t('catalog.max')">
          <el-switch
            v-model="replaceForm.supports_max"
            @change="onReplaceEdit"
          />
        </el-form-item>
      </el-form>
      <div
        v-if="replacePlan"
        class="replace-plan"
        data-test="replace-plan"
      >
        <p v-if="replacePlan.created">
          {{ t('catalog.replaceCreated') }}
        </p>
        <p v-if="replacePlan.default_before !== replacePlan.default_after">
          {{ t('catalog.replaceDefaultChange', { from: replacePlan.default_before ?? '—', to: replacePlan.default_after ?? '—' }) }}
        </p>
        <h5>{{ t('catalog.replaceBots', { n: replacePlan.bots.length }) }}</h5>
        <ul v-if="replacePlan.bots.length">
          <li
            v-for="b in replacePlan.bots"
            :key="b.id"
          >
            {{ b.name }}: {{ b.model }}
            <span v-if="b.effort_level !== b.new_effort_level">
              ({{ t('catalog.replaceEffort', { from: b.effort_level, to: b.new_effort_level }) }})
            </span>
          </li>
        </ul>
        <p v-else>
          {{ t('catalog.replaceNone') }}
        </p>
        <h5>{{ t('catalog.replaceRelays', { n: replacePlan.relays.length }) }}</h5>
        <ul v-if="replacePlan.relays.length">
          <li
            v-for="r in replacePlan.relays"
            :key="r.id"
          >
            {{ r.name }}: {{ r.after.join(', ') }}
          </li>
        </ul>
        <p v-else>
          {{ t('catalog.replaceNone') }}
        </p>
      </div>
      <template #footer>
        <el-button @click="replaceVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          data-test="replace-preview"
          :loading="replacing && !replacePlan"
          @click="submitReplace(true)"
        >
          {{ t('catalog.replacePreview') }}
        </el-button>
        <el-button
          type="primary"
          data-test="replace-apply"
          :disabled="!replacePlan"
          :loading="replacing && !!replacePlan"
          @click="submitReplace(false)"
        >
          {{ t('catalog.replaceApply') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.panel-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.provider-group { margin-bottom: 16px; }
.provider-group h4 { margin: 8px 0; color: var(--el-text-color-secondary); }
.replace-hint { margin: 0 0 12px; color: var(--el-text-color-secondary); }
.replace-note { margin-left: 8px; font-size: 12px; color: var(--el-text-color-secondary); }
.replace-plan { padding: 8px 12px; border-radius: 4px; background: var(--el-fill-color-light); }
.replace-plan h5 { margin: 8px 0 4px; }
.replace-plan ul { margin: 0; padding-left: 20px; }
</style>
