<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import SkillEditorDialog from '@/components/skills/SkillEditorDialog.vue'
import SkillPresetDialog from '@/components/skills/SkillPresetDialog.vue'
import SkillSourceDialog from '@/components/skills/SkillSourceDialog.vue'
import { useListQuery } from '@/composables/useListQuery'
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { skills, allSkills, type Skill, type Source, type Preset } from '@/api/skills'
import { useAuthStore } from '@/stores/auth'
const { t } = useI18n()
const auth = useAuthStore()
const manager = computed(() => ['ai_committee', 'platform_admin'].includes(auth.user?.role ?? ''))
const rows = ref<Skill[]>([]), sources = ref<Source[]>([]), presets = ref<Preset[]>([])
const query = ref('')
const activeTab = ref('catalog'), sourceFilter = ref(''), syncing = ref(''), syncResult = ref('')
const skillDialog = ref<InstanceType<typeof SkillEditorDialog>>()
const sourceDialog = ref<InstanceType<typeof SkillSourceDialog>>()
const presetDialog = ref<InstanceType<typeof SkillPresetDialog>>()
const filtered = computed(() => rows.value.filter(row => (!sourceFilter.value || row.source_id === sourceFilter.value) && `${row.name} ${row.description}`.toLowerCase().includes(query.value.toLowerCase())))
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
const fail = (e: unknown) => ElMessage.error(errorMessage(e))
const listLoading = ref(false), listError = ref('')
const { persist } = useListQuery({ keyword: query })
async function load() { listLoading.value = true; listError.value = ''; try { const [a, b] = await Promise.all([allSkills(), skills.sources()]); rows.value = a; sources.value = b; if (manager.value) presets.value = await skills.presets() } catch (e) { listError.value = errorMessage(e); fail(e) } finally { listLoading.value = false } }
/** 新建技能时预选来源：正在按来源筛选就用它，只有一个来源时直接选中。 */
function edit(row: Skill | null) { skillDialog.value?.open(row, sourceFilter.value || (sources.value.length === 1 ? sources.value[0]!.id : '')) }
function editSource(row: Source | null) { sourceDialog.value?.open(row) }
function editPreset(row: Preset | null) { presetDialog.value?.open(row) }
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

    <SkillEditorDialog
      ref="skillDialog"
      :sources="sources"
      :presets="presets"
      @saved="load"
    />

    <SkillSourceDialog
      ref="sourceDialog"
      @saved="load"
    />
    <SkillPresetDialog
      ref="presetDialog"
      @saved="load"
    />
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
@media (max-width: 700px) {
  .catalog-toolbar { align-items: stretch; flex-direction: column; }
  .catalog-filters { max-width: none; flex-wrap: wrap; }
  .catalog-toolbar > .row-actions { justify-content: flex-end; }
  .section-toolbar { flex-wrap: wrap; gap: 12px; }
}
</style>
