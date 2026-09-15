<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import SkillEnvEditor from '@/components/SkillEnvEditor.vue'
import type { EnvEntry } from '@/utils/dotenv'
import { useListQuery } from '@/composables/useListQuery'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { skills, allSkills, type Skill, type SkillInput, type Source, type Preset } from '@/api/skills'
import { useAuthStore } from '@/stores/auth'
const { t } = useI18n()
const auth = useAuthStore()
const manager = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))
const rows = ref<Skill[]>([]), sources = ref<Source[]>([]), presets = ref<Preset[]>([])
const busy = ref(false), visible = ref(false), sourceVisible = ref(false), presetVisible = ref(false), query = ref('')
const selected = ref<Skill | null>(null), selectedSource = ref<Source | null>(null)
const activeTab = ref('catalog'), sourceFilter = ref(''), syncing = ref(''), syncResult = ref('')
const advanced = ref<string[]>([])
const envEditor = ref<InstanceType<typeof SkillEnvEditor>>()
const selectedPreset = ref(false)
const filtered = computed(() => rows.value.filter(row => (!sourceFilter.value || row.source_id === sourceFilter.value) && `${row.name} ${row.description}`.toLowerCase().includes(query.value.toLowerCase())))
const currentSource = computed(() => sources.value.find(source => source.id === form.source_id))
const sourceName = (id: string) => sources.value.find(source => source.id === id)?.label ?? '—'
const sourceCount = (id: string) => rows.value.filter(row => row.source_id === id).length
function showSource(row: Source) { sourceFilter.value = row.id; query.value = ''; activeTab.value = 'catalog'; persist() }
async function syncSource(row: Source) {
  syncing.value = row.id; syncResult.value = ''
  try {
    const result = await skills.sourceSync(row)
    syncResult.value = t('skillEditor.syncResult', result)
    await load(); showSource(row)
  } catch (e) { fail(e) } finally { syncing.value = '' }
}
const empty = (): SkillInput => ({ name: '', source_id: '', description: '', category: null, security_level: 'public', version: null, env_groups: [], selectable_env_groups: {}, data_sources: null, default_data_source: null, doris_enabled_groups: [], user_env_vars: {}, install_type: 'git', external_repo_url: null, security_prompt_template: null, enabled: false })
const form = reactive<SkillInput>(empty())
const selectable = ref<string[]>([]), mcp = ref('')
const fields = ref<{ key: string; label: string; placeholder: string; required: boolean }[]>([])
const sourceForm = reactive({ key: '', label: '', git_url: '', categories: {} as Record<string, string>, sort_order: 0, access_token: '', remove_access_token: false })
const presetForm = reactive<Preset>({ group_key: '', label: '', vars: {}, tags: [], version: 0 })
const variables = ref<{ key: string; value: string }[]>([])
const fail = (e: unknown) => ElMessage.error(errorMessage(e))
const listLoading = ref(false), listError = ref('')
const { persist } = useListQuery({ keyword: query })
async function load() { listLoading.value = true; listError.value = ''; try { const [a, b] = await Promise.all([allSkills(), skills.sources()]); rows.value = a; sources.value = b; if (manager.value) presets.value = await skills.presets() } catch (e) { listError.value = errorMessage(e); fail(e) } finally { listLoading.value = false } }
function edit(row: Skill | null) {
  selected.value = row
  const base = empty()
  for (const key of Object.keys(base) as (keyof SkillInput)[]) { if (row) Object.assign(base, { [key]: row[key] }) }
  Object.assign(form, JSON.parse(JSON.stringify(base)))
  selectable.value = Object.keys(row?.selectable_env_groups ?? {})
  fields.value = Object.entries(row?.user_env_vars ?? {}).map(([key, value]) => ({ key, ...value }))
  if (!row) base.source_id = sourceFilter.value || (sources.value.length === 1 ? sources.value[0]!.id : '')
  form.source_id = base.source_id
  advanced.value = row?.data_sources || row?.doris_enabled_groups?.length || Object.keys(row?.selectable_env_groups ?? {}).length ? ['database'] : []
  mcp.value = ''; visible.value = true
}
async function save() {
  busy.value = true
  try {
    if (!form.name.trim() || !form.source_id) throw new Error(t('skillEditor.requiredBasics'))
    const body: SkillInput = { ...form, selectable_env_groups: Object.fromEntries(selectable.value.map(key => [key, form.selectable_env_groups[key] ?? presets.value.find(p => p.group_key === key)?.label ?? key])), user_env_vars: Object.fromEntries(fields.value.map(({ key, ...value }) => [key, value])), mcp_config: mcp.value.trim() ? JSON.parse(mcp.value) : null }
    if (new Set(fields.value.map(f => f.key)).size !== fields.value.length) throw new Error(t('skill.duplicateKey'))
    await skills.save(selected.value, body); visible.value = false; mcp.value = ''; await load()
  } catch (e) { fail(e) } finally { busy.value = false }
}
function editSource(row: Source | null) { selectedSource.value = row; Object.assign(sourceForm, { key: row?.key ?? '', label: row?.label ?? '', git_url: row?.git_url ?? '', categories: row?.categories ?? {}, sort_order: row?.sort_order ?? 0, access_token: '', remove_access_token: false }); sourceVisible.value = true }
async function saveSource() { busy.value = true; try { await skills.sourceSave(selectedSource.value, { ...sourceForm, git_url: sourceForm.git_url || null }); sourceVisible.value = false; sourceForm.access_token = ''; await load() } catch (e) { fail(e) } finally { busy.value = false } }
function editPreset(row: Preset | null) { selectedPreset.value = !!row; Object.assign(presetForm, row ?? { group_key: '', label: '', vars: {}, tags: [], version: 0 }); variables.value = Object.entries(row?.vars ?? {}).map(([key, value]) => ({ key, value })); presetVisible.value = true }
async function savePreset() {
  busy.value = true
  try {
    if (!presetForm.group_key.trim() || !presetForm.label.trim()) throw new Error(t('skillEditor.presetRequired'))
    const values: EnvEntry[] = envEditor.value!.read()
    await skills.presetSave({ ...presetForm, vars: Object.fromEntries(values.map(v => [v.key, v.value])) })
    presetVisible.value = false; variables.value = []; await load()
  } catch (e) { fail(e) } finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <section class="skills-page">
    <header class="cm-page-header">
      <h2>{{ t('menu.skills') }}</h2>
      <p class="cm-page-intro">
        {{ t('skillEditor.intro') }}
      </p>
    </header>
    <LoadState
      :loading="listLoading"
      :error="listError"
      @retry="load"
    />
    <el-alert
      v-if="syncResult"
      :title="syncResult"
      type="success"
      show-icon
      @close="syncResult = ''"
    />
    <el-tabs v-model="activeTab">
      <el-tab-pane
        name="catalog"
        :label="t('skill.catalog')"
      >
        <div class="catalog-toolbar">
          <div class="catalog-filters">
            <el-input
              v-model="query"
              :aria-label="t('common.search')"
              :placeholder="t('common.search')"
              clearable
              @change="persist"
            />
            <el-select
              v-model="sourceFilter"
              :aria-label="t('skill.sources')"
              :placeholder="t('skillEditor.allSources')"
              clearable
            >
              <el-option
                v-for="source in sources"
                :key="source.id"
                :label="source.label"
                :value="source.id"
              />
            </el-select>
          </div>
          <div class="row-actions">
            <el-button @click="load">
              {{ t('common.refresh') }}
            </el-button>
            <el-button
              v-if="manager"
              type="primary"
              :disabled="!sources.length"
              @click="edit(null)"
            >
              {{ t('skillEditor.newSkill') }}
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="manager && !sources.length && !listLoading"
          type="info"
          :closable="false"
        >
          <p>{{ t('skillEditor.noSources') }}</p>
          <el-button @click="activeTab = 'sources'; editSource(null)">
            {{ t('skillEditor.newSource') }}
          </el-button>
        </el-alert>
        <el-table
          :data="filtered"
          :empty-text="t('skillEditor.emptyCatalog')"
        >
          <el-table-column
            prop="name"
            :label="t('common.name')"
            min-width="180"
          />
          <el-table-column
            prop="description"
            :label="t('common.description')"
            min-width="260"
          >
            <template #default="{ row }">
              <el-tooltip
                v-if="row.description"
                placement="top-start"
                effect="light"
                :show-after="250"
                :hide-after="150"
                :enterable="true"
                popper-class="skill-description-tooltip"
              >
                <template #content>
                  <div class="skill-description-content">
                    {{ row.description }}
                  </div>
                </template>
                <span
                  class="skill-description-preview"
                  tabindex="0"
                >{{ row.description }}</span>
              </el-tooltip>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('skill.sources')"
            min-width="150"
          >
            <template #default="{ row }">
              {{ sourceName(row.source_id) }}
            </template>
          </el-table-column>
          <el-table-column
            prop="version"
            :label="t('skill.packageVersion')"
            min-width="115"
          />
          <el-table-column
            :label="t('skill.security')"
            min-width="120"
          >
            <template #default="{ row }">
              {{ t(`skill.${row.security_level}`) }}
            </template>
          </el-table-column>
          <el-table-column
            :label="t('common.status')"
            min-width="100"
          >
            <template #default="{ row }">
              <el-tag
                :type="row.enabled ? 'success' : 'info'"
                effect="plain"
              >
                {{ t(row.enabled ? 'common.enabled' : 'common.disabled') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            v-if="manager"
            :label="t('common.actions')"
            width="90"
            fixed="right"
          >
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                @click="edit(row)"
              >
                {{ t('common.edit') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
      <el-tab-pane
        v-if="manager"
        name="sources"
        :label="t('skill.sources')"
      >
        <div class="section-toolbar">
          <div>
            <h3>{{ t('skillEditor.sourceTitle') }}</h3><p class="hint">
              {{ t('skillEditor.sourceHint') }}
            </p>
          </div>
          <el-button
            type="primary"
            @click="editSource(null)"
          >
            {{ t('skillEditor.newSource') }}
          </el-button>
        </div>
        <el-table :data="sources">
          <el-table-column
            :label="t('common.name')"
            min-width="150"
          >
            <template #default="{ row }">
              <strong>{{ row.label }}</strong><div class="hint">
                {{ row.key }}
              </div>
            </template>
          </el-table-column>
          <el-table-column
            prop="git_url"
            :label="t('skill.repository')"
            min-width="300"
          />
          <el-table-column
            :label="t('skillEditor.linkedSkills')"
            width="130"
          >
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                @click="showSource(row)"
              >
                {{ t('skillEditor.skillCount', { count: sourceCount(row.id) }) }}
              </el-button>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('common.actions')"
            min-width="265"
            fixed="right"
          >
            <template #default="{ row }">
              <div class="row-actions">
                <el-button
                  link
                  type="primary"
                  :loading="syncing === row.id"
                  :disabled="!row.git_url || !!syncing && syncing !== row.id"
                  @click="syncSource(row)"
                >
                  {{ t('skillEditor.sync') }}
                </el-button>
                <el-button
                  link
                  @click="showSource(row)"
                >
                  {{ t('skillEditor.viewSkills') }}
                </el-button>
                <el-button
                  link
                  @click="editSource(row)"
                >
                  {{ t('common.edit') }}
                </el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <p class="hint after-table">
          {{ t('skillEditor.syncHint') }}
        </p>
      </el-tab-pane>
      <el-tab-pane
        v-if="manager"
        name="presets"
        :label="t('skill.presets')"
      >
        <div class="section-toolbar">
          <div>
            <h3>{{ t('skillEditor.presetTitle') }}</h3><p class="hint">
              {{ t('skillEditor.presetHint') }}
            </p>
          </div>
          <el-button
            type="primary"
            @click="editPreset(null)"
          >
            {{ t('skillEditor.newPreset') }}
          </el-button>
        </div>
        <el-table :data="presets">
          <el-table-column
            prop="label"
            :label="t('common.name')"
            min-width="160"
          />
          <el-table-column
            prop="group_key"
            :label="t('infra.key')"
            min-width="180"
          />
          <el-table-column
            :label="t('skillEditor.envValues')"
            min-width="240"
          >
            <template #default="{ row }">
              <div class="preset-keys">
                <el-tag
                  v-for="key in Object.keys(row.vars)"
                  :key="key"
                  type="info"
                  effect="plain"
                >
                  {{ key }}
                </el-tag><span v-if="!Object.keys(row.vars).length">—</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('common.actions')"
            width="90"
          >
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                @click="editPreset(row)"
              >
                {{ t('common.edit') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <p class="hint after-table">
          {{ t('skill.presetHint') }}
        </p>
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="visible"
      :close-on-click-modal="false"
      :title="t(selected ? 'skillEditor.editSkill' : 'skillEditor.newSkill')"
      width="880px"
      destroy-on-close
      @closed="mcp = ''"
    >
      <el-form
        class="skill-form"
        label-position="top"
        :disabled="busy"
      >
        <section class="form-section">
          <h3>{{ t('skillEditor.basics') }}</h3>
          <div class="form-grid">
            <el-form-item
              :label="t('skillEditor.skillName')"
              required
            >
              <el-input
                v-model="form.name"
                placeholder="mysql-query"
                maxlength="100"
              />
              <span class="hint">{{ t('skillEditor.nameHint') }}</span>
            </el-form-item>
            <el-form-item
              :label="t('skill.sources')"
              required
            >
              <el-select
                v-model="form.source_id"
                filterable
              >
                <el-option
                  v-for="source in sources"
                  :key="source.id"
                  :label="source.label"
                  :value="source.id"
                />
              </el-select>
              <span class="hint">{{ t('skillEditor.belongsTo') }}</span>
            </el-form-item>
            <el-form-item
              class="full-width"
              :label="t('common.description')"
            >
              <el-input
                v-model="form.description"
                type="textarea"
                :rows="2"
                :placeholder="t('skillEditor.descriptionHint')"
              />
            </el-form-item>
          </div>
        </section>
        <section class="form-section">
          <h3>{{ t('skillEditor.installation') }}</h3>
          <div class="form-grid">
            <el-form-item :label="t('skill.installType')">
              <el-radio-group v-model="form.install_type">
                <el-radio-button value="git">
                  {{ t('skillEditor.git') }}
                </el-radio-button><el-radio-button value="mcp">
                  MCP
                </el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item :label="t('skill.packageVersion')">
              <el-input
                v-model="form.version"
                :placeholder="t('skillEditor.versionHint')"
              />
            </el-form-item>
            <template v-if="form.install_type === 'git'">
              <div class="repository-summary full-width">
                <span>{{ t('skillEditor.effectiveRepository') }}</span><strong>{{ form.external_repo_url || currentSource?.git_url || t('skillEditor.missingRepository') }}</strong>
              </div>
              <el-form-item
                class="full-width"
                :label="t('skill.repositoryOverride')"
              >
                <el-input
                  v-model="form.external_repo_url"
                  clearable
                  :placeholder="currentSource?.git_url || 'git@host:team/skills.git'"
                  @change="form.external_repo_url ||= null"
                />
              </el-form-item>
            </template>
            <el-form-item
              v-else
              class="full-width"
              :label="t('skill.mcpConfig')"
              :required="!selected?.has_mcp_config"
            >
              <el-input
                v-model="mcp"
                class="code-input"
                type="textarea"
                :rows="6"
                :placeholder="selected?.has_mcp_config ? t('skill.preserveConfig') : '{}'"
              />
            </el-form-item>
          </div>
        </section>
        <section class="form-section">
          <h3>{{ t('skillEditor.access') }}</h3>
          <div class="form-grid">
            <el-form-item :label="t('skill.security')">
              <el-radio-group v-model="form.security_level">
                <el-radio value="public">
                  {{ t('skill.public') }}
                </el-radio><el-radio value="internal">
                  {{ t('skill.internal') }}
                </el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item :label="t('skillEditor.fixedPresets')">
              <el-select
                v-model="form.env_groups"
                multiple
                filterable
                :placeholder="t('skillEditor.optional')"
              >
                <el-option
                  v-for="preset in presets"
                  :key="preset.group_key"
                  :label="preset.label"
                  :value="preset.group_key"
                />
              </el-select>
              <span class="hint">{{ t('skillEditor.fixedHint') }}</span>
            </el-form-item>
            <el-form-item
              v-if="form.security_level === 'internal'"
              class="full-width"
              :label="t('skillEditor.defaultPolicy')"
            >
              <el-input
                v-model="form.security_prompt_template"
                type="textarea"
                :rows="3"
                :placeholder="t('skillEditor.policyHint')"
              />
            </el-form-item>
          </div>
          <el-collapse v-model="advanced">
            <el-collapse-item
              name="database"
              :title="t('skillEditor.databaseAdvanced')"
            >
              <p class="hint">
                {{ t('skillEditor.databaseHint') }}
              </p>
              <div class="form-grid">
                <el-form-item :label="t('skill.selectableGroups')">
                  <el-select
                    v-model="selectable"
                    multiple
                    filterable
                    allow-create
                  >
                    <el-option
                      v-for="preset in presets"
                      :key="preset.group_key"
                      :label="preset.label"
                      :value="preset.group_key"
                    />
                  </el-select>
                </el-form-item>
                <el-form-item :label="t('skill.dataSource')">
                  <el-select
                    v-model="form.default_data_source"
                    clearable
                    @change="form.data_sources = form.default_data_source ? { mysql: 'MySQL', doris: 'Doris' } : null"
                  >
                    <el-option
                      label="MySQL"
                      value="mysql"
                    /><el-option
                      label="Doris"
                      value="doris"
                    />
                  </el-select>
                </el-form-item>
                <el-form-item
                  v-if="form.data_sources"
                  class="full-width"
                  :label="t('skill.dorisGroups')"
                >
                  <el-select
                    v-model="form.doris_enabled_groups"
                    multiple
                  >
                    <el-option
                      v-for="key in selectable"
                      :key="key"
                      :label="key"
                      :value="key"
                    />
                  </el-select>
                </el-form-item>
              </div>
            </el-collapse-item>
          </el-collapse>
        </section>
        <section class="form-section">
          <div class="section-toolbar compact">
            <div>
              <h3>{{ t('skill.userFields') }}</h3><p class="hint">
                {{ t('skillEditor.userFieldsHint') }}
              </p>
            </div><el-button @click="fields.push({ key: '', label: '', placeholder: '', required: false })">
              {{ t('skillEditor.addField') }}
            </el-button>
          </div>
          <p
            v-if="!fields.length"
            class="hint empty-fields"
          >
            {{ t('skillEditor.noUserFields') }}
          </p>
          <div
            v-for="(field, index) in fields"
            :key="index"
            class="user-field"
          >
            <div class="form-grid">
              <el-form-item
                :label="t('skillEditor.envKey')"
                required
              >
                <el-input
                  v-model="field.key"
                  placeholder="API_KEY"
                />
              </el-form-item>
              <el-form-item :label="t('skillEditor.fieldLabel')">
                <el-input
                  v-model="field.label"
                  :placeholder="t('skillEditor.fieldLabelHint')"
                />
              </el-form-item>
              <el-form-item
                class="full-width"
                :label="t('skillEditor.fieldHint')"
              >
                <el-input
                  v-model="field.placeholder"
                  :placeholder="t('skillEditor.fieldHintExample')"
                />
              </el-form-item>
            </div>
            <div class="field-actions">
              <el-checkbox v-model="field.required">
                {{ t('skill.required') }}
              </el-checkbox><el-button
                link
                type="danger"
                @click="fields.splice(index, 1)"
              >
                {{ t('common.delete') }}
              </el-button>
            </div>
          </div>
        </section>
        <div class="publish-setting">
          <div>
            <strong>{{ t('skillEditor.publish') }}</strong><p class="hint">
              {{ t('skillEditor.publishHint') }}
            </p>
          </div><el-switch
            v-model="form.enabled"
            :aria-label="t('skillEditor.publish')"
          />
        </div>
      </el-form>
      <template #footer>
        <el-button @click="visible = false">
          {{ t('common.cancel') }}
        </el-button><el-button
          type="primary"
          :loading="busy"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="sourceVisible"
      :close-on-click-modal="false"
      :title="t(selectedSource ? 'skillEditor.editSource' : 'skillEditor.newSource')"
      width="680px"
      @closed="sourceForm.access_token = ''"
    >
      <p class="hint dialog-intro">
        {{ t('skillEditor.sourceDialogHint') }}
      </p>
      <el-form
        label-position="top"
        :disabled="busy"
      >
        <div class="form-grid">
          <el-form-item
            :label="t('common.name')"
            required
          >
            <el-input
              v-model="sourceForm.label"
              :placeholder="t('skillEditor.sourceLabelHint')"
            />
          </el-form-item>
          <el-form-item
            :label="t('infra.key')"
            required
          >
            <el-input
              v-model="sourceForm.key"
              placeholder="tools-marketplace"
            />
          </el-form-item>
          <el-form-item
            class="full-width"
            :label="t('skill.repository')"
          >
            <el-input
              v-model="sourceForm.git_url"
              placeholder="https://github.com/example/skills.git"
            /><span class="hint">{{ t('skillEditor.repositoryHint') }}</span>
          </el-form-item>
          <el-form-item
            class="full-width"
            :label="t('skillEditor.projectToken')"
          >
            <el-input
              v-model="sourceForm.access_token"
              type="password"
              show-password
              autocomplete="new-password"
              :disabled="sourceForm.remove_access_token"
              :placeholder="t(selectedSource?.has_access_token ? 'skillEditor.tokenPreserve' : 'skillEditor.tokenPlaceholder')"
            />
            <span class="hint">{{ t('skillEditor.tokenHint') }}</span>
            <el-checkbox
              v-if="selectedSource?.has_access_token"
              v-model="sourceForm.remove_access_token"
              @change="sourceForm.access_token = ''"
            >
              {{ t('skillEditor.removeToken') }}
            </el-checkbox>
          </el-form-item>
          <el-form-item :label="t('infra.sortOrder')">
            <el-input-number
              v-model="sourceForm.sort_order"
              :min="0"
              :max="10000"
            />
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="sourceVisible = false">
          {{ t('common.cancel') }}
        </el-button><el-button
          type="primary"
          :loading="busy"
          @click="saveSource"
        >
          {{ t('skillEditor.saveSource') }}
        </el-button>
      </template>
    </el-dialog>
    <el-dialog
      v-model="presetVisible"
      :close-on-click-modal="false"
      :title="t(selectedPreset ? 'skillEditor.editPreset' : 'skillEditor.newPreset')"
      width="780px"
      destroy-on-close
      @closed="variables = []"
    >
      <el-form
        label-position="top"
        :disabled="busy"
      >
        <div class="form-grid">
          <el-form-item
            :label="t('common.name')"
            required
          >
            <el-input
              v-model="presetForm.label"
              :placeholder="t('skillEditor.presetLabelHint')"
            />
          </el-form-item>
          <el-form-item
            :label="t('infra.key')"
            required
          >
            <el-input
              v-model="presetForm.group_key"
              :disabled="presetForm.version > 0"
              placeholder="db_erp"
            />
          </el-form-item>
        </div>
        <SkillEnvEditor
          ref="envEditor"
          v-model="variables"
        />
        <p class="hint after-table">
          {{ t('skill.presetHint') }}
        </p>
      </el-form>
      <template #footer>
        <el-button @click="presetVisible = false">
          {{ t('common.cancel') }}
        </el-button><el-button
          type="primary"
          :loading="busy"
          @click="savePreset"
        >
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
<style>
.el-popper.skill-description-tooltip {
  max-width: min(480px, calc(100vw - 32px));
  padding: 14px 16px;
  border: 1px solid var(--cm-border);
  border-radius: 10px;
  background: var(--el-bg-color-overlay);
  color: var(--el-text-color-primary);
  box-shadow: 0 8px 28px rgb(0 0 0 / 12%);
}
.skill-description-content {
  max-height: min(360px, 50vh);
  overflow-y: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 13px;
  line-height: 1.75;
  user-select: text;
}
</style>
<style scoped>
.skill-description-preview { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: help; border-radius: 3px; }
.skill-description-preview:focus-visible { outline: 2px solid var(--cm-brand); outline-offset: -2px; }
.catalog-toolbar, .section-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
.catalog-filters { display: flex; gap: 12px; flex: 1; max-width: 620px; }
.catalog-filters > .el-input { flex: 1.4; min-width: 140px; }
.catalog-filters > .el-select { flex: 1; min-width: 160px; }
.row-actions { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.row-actions .el-button + .el-button { margin-left: 0; }
.section-toolbar { align-items: flex-start; }
.section-toolbar h3 { margin: 0 0 6px; }
.hint { color: var(--cm-muted); font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
p.hint { margin: 0; max-width: 76ch; }
.after-table { margin-top: 16px !important; }
.preset-keys { display: flex; flex-wrap: wrap; gap: 6px; }
.form-section { padding-bottom: 24px; margin-bottom: 24px; border-bottom: 1px solid var(--cm-border); }
.form-section > h3 { margin: 0 0 20px; }
.form-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); column-gap: 24px; }
.form-grid > * { min-width: 0; }
.full-width { grid-column: 1 / -1; }
.form-grid .el-select { width: 100%; }
.repository-summary { display: flex; flex-direction: column; gap: 4px; padding: 12px 16px; background: var(--cm-brand-soft); border-radius: 8px; margin-bottom: 18px; font-size: 13px; overflow-wrap: anywhere; }
.repository-summary span { color: var(--cm-muted); }
.repository-summary strong { font-weight: 500; }
.dialog-intro { margin-bottom: 24px !important; }
.compact { margin-bottom: 16px; }
.empty-fields { padding: 14px 16px; background: var(--el-fill-color-light); border-radius: 8px; }
.user-field { padding: 16px; border: 1px solid var(--cm-border); border-radius: 8px; margin-bottom: 12px; }
.user-field .el-form-item { margin-bottom: 14px; }
.field-actions, .publish-setting { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.publish-setting { padding: 0 0 4px; }
.code-input :deep(textarea) { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
@media (max-width: 700px) {
  .catalog-toolbar { align-items: stretch; flex-direction: column; }
  .catalog-filters { max-width: none; flex-wrap: wrap; }
  .catalog-toolbar > .row-actions { justify-content: flex-end; }
  .section-toolbar { flex-wrap: wrap; gap: 12px; }
  .form-grid { grid-template-columns: minmax(0, 1fr); }
  .form-section { padding-bottom: 20px; margin-bottom: 20px; }
}
</style>
