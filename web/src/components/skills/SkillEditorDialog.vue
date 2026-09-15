<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { skills, type Preset, type Skill, type SkillInput, type Source } from '@/api/skills'

const props = defineProps<{ sources: Source[]; presets: Preset[] }>()
const emit = defineEmits<{ saved: [] }>()
const { t } = useI18n()
const busy = ref(false), visible = ref(false)
const selected = ref<Skill | null>(null)
const advanced = ref<string[]>([])
const empty = (): SkillInput => ({ name: '', source_id: '', description: '', category: null, security_level: 'public', version: null, env_groups: [], selectable_env_groups: {}, data_sources: null, default_data_source: null, doris_enabled_groups: [], user_env_vars: {}, install_type: 'git', external_repo_url: null, security_prompt_template: null, enabled: false })
const form = reactive<SkillInput>(empty())
const selectable = ref<string[]>([]), mcp = ref('')
const fields = ref<{ key: string; label: string; placeholder: string; required: boolean }[]>([])
const currentSource = computed(() => props.sources.find(source => source.id === form.source_id))
const fail = (e: unknown) => ElMessage.error(errorMessage(e))

/** 打开技能编辑器：row 为空是新建，此时来源预选为 defaultSourceId。 */
function open(row: Skill | null, defaultSourceId: string) {
  selected.value = row
  const base = empty()
  for (const key of Object.keys(base) as (keyof SkillInput)[]) { if (row) Object.assign(base, { [key]: row[key] }) }
  Object.assign(form, JSON.parse(JSON.stringify(base)))
  selectable.value = Object.keys(row?.selectable_env_groups ?? {})
  fields.value = Object.entries(row?.user_env_vars ?? {}).map(([key, value]) => ({ key, ...value }))
  if (!row) base.source_id = defaultSourceId
  form.source_id = base.source_id
  advanced.value = row?.data_sources || row?.doris_enabled_groups?.length || Object.keys(row?.selectable_env_groups ?? {}).length ? ['database'] : []
  mcp.value = ''; visible.value = true
}
async function save() {
  busy.value = true
  try {
    if (!form.name.trim() || !form.source_id) throw new Error(t('skillEditor.requiredBasics'))
    const body: SkillInput = { ...form, selectable_env_groups: Object.fromEntries(selectable.value.map(key => [key, form.selectable_env_groups[key] ?? props.presets.find(p => p.group_key === key)?.label ?? key])), user_env_vars: Object.fromEntries(fields.value.map(({ key, ...value }) => [key, value])), mcp_config: mcp.value.trim() ? JSON.parse(mcp.value) : null }
    if (new Set(fields.value.map(f => f.key)).size !== fields.value.length) throw new Error(t('skill.duplicateKey'))
    await skills.save(selected.value, body); visible.value = false; mcp.value = ''; emit('saved')
  } catch (e) { fail(e) } finally { busy.value = false }
}
defineExpose({ open, save, form })
</script>

<template>
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
</template>

<style scoped src="./skillForm.css"></style>
