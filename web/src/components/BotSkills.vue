<script setup lang="ts">
import LoadState from '@/components/LoadState.vue'
import { computed, ref, reactive, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { allSkills, skills, type Skill, type Installed, type Approval, type InstallInput } from '@/api/skills'
const props = defineProps<{ botId: string }>()
const emit = defineEmits<{ saved: [] }>()
const { t } = useI18n()
const loadError = ref('')
const visible = ref(false), editing = ref(false), busy = ref(false)
const catalog = ref<Skill[]>([]), rows = ref<Installed[]>([]), pending = ref<Approval[]>([])
const selected = ref<Skill | null>(null)
const databaseOptions = computed(() => Object.entries(selected.value?.selectable_env_groups ?? {}).map(([value, label]) => ({ value, label })))
const sourceOptions = computed(() => Object.entries(selected.value?.data_sources ?? {}).map(([value, label]) => ({ value, label })))
const form = reactive<InstallInput>({ selected_env_groups: [], data_source: null, user_env_vars: {}, requested_security_prompt: null, reinstall_code: true })
const fail = (e: unknown) => ElMessage.error(e instanceof Error ? e.message : String(e))
async function load() {
  loadError.value = ''
  busy.value = true
  try { const [available, installed] = await Promise.all([allSkills(), skills.installed(props.botId)]); catalog.value = available.filter(s => s.enabled); rows.value = installed.items; pending.value = installed.pending_approvals }
  catch (e) { loadError.value = e instanceof Error ? e.message : String(e); fail(e) } finally { busy.value = false }
}
watch(visible, value => { if (value) void load() })
function edit(skill: Skill) {
  selected.value = skill
  const current = rows.value.find(row => row.skill_id === skill.id)
  Object.assign(form, { selected_env_groups: current?.selected_env_groups.filter(key => key in skill.selectable_env_groups) ?? [], data_source: skill.default_data_source, user_env_vars: Object.fromEntries(Object.entries(current?.user_env_vars ?? {}).filter(([key]) => key in skill.user_env_vars)), requested_security_prompt: current?.security_prompt || skill.security_prompt_template || '', reinstall_code: true })
  editing.value = true
}
async function install() {
  if (!selected.value || busy.value) return
  busy.value = true
  try { const result = await skills.install(props.botId, selected.value.id, form); ElMessage.success(t(`skill.status.${result.status}`)); editing.value = false; await load(); emit('saved') } catch (e) { fail(e) } finally { busy.value = false }
}
async function remove(row: Installed) {
  try { await ElMessageBox.confirm(t('skill.removeHint'), t('common.confirm')) } catch { return }
  busy.value = true
  try { await skills.uninstall(props.botId, row); await load(); emit('saved') } catch (e) { fail(e) } finally { busy.value = false }
}
</script>
<template>
  <el-button
    data-test="skills-open"
    @click="visible = true"
  >
    {{ t('menu.skills') }}
  </el-button>
  <el-dialog
    v-model="visible"
    :title="t('menu.skills')"
    width="900px"
    destroy-on-close
  >
    <el-alert
      :title="t('skill.installHint')"
      :closable="false"
      type="info"
    />
    <el-button
      :loading="busy"
      @click="load"
    >
      {{ t('common.refresh') }}
    </el-button>
    <LoadState
      :error="loadError"
      @retry="load"
    />
    <el-table :data="rows">
      <el-table-column
        min-width="140"
        prop="name"
        :label="t('common.name')"
      />
      <el-table-column
        min-width="140"
        :label="t('common.status')"
      >
        <template #default="{ row }">
          {{ t(`skill.status.${row.status}`) }}<p v-if="row.error_message">
            {{ row.error_message }}
          </p>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        prop="version"
        :label="t('skill.packageVersion')"
      />
      <el-table-column
        min-width="140"
        :label="t('common.actions')"
      >
        <template #default="{ row }">
          <el-button
            v-if="row.status !== 'uninstalled'"
            link
            type="danger"
            :disabled="busy"
            @click="remove(row)"
          >
            {{ t('skill.remove') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-alert
      v-for="row in pending"
      :key="row.id"
      :title="`${catalog.find(s => s.id === row.skill_id)?.name ?? row.skill_id}: ${t('skill.status.pending_approval')}`"
      :closable="false"
      type="warning"
    />
    <h3>{{ t('skill.available') }}</h3>
    <el-table :data="catalog">
      <el-table-column
        min-width="140"
        prop="name"
        :label="t('common.name')"
      />
      <el-table-column
        min-width="140"
        prop="description"
        :label="t('common.description')"
      />
      <el-table-column
        min-width="140"
        :label="t('skill.security')"
      >
        <template #default="{ row }">
          {{ t(`skill.${row.security_level}`) }}
        </template>
      </el-table-column>
      <el-table-column min-width="140">
        <template #default="{ row }">
          <el-button
            :disabled="busy || pending.some(a => a.skill_id === row.id) || rows.some(r => r.skill_id === row.id && r.status === 'installing')"
            :data-test="`skill-install-${row.id}`"
            @click="edit(row)"
          >
            {{ t('skill.configureInstall') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>
  <el-dialog
    v-model="editing"
    :close-on-click-modal="false"
    :title="selected?.name"
    width="640px"
    destroy-on-close
  >
    <el-form
      v-if="selected"
      label-position="top"
    >
      <el-form-item
        v-if="Object.keys(selected.selectable_env_groups).length"
        :label="t('skill.databases')"
      >
        <el-checkbox-group v-model="form.selected_env_groups">
          <el-checkbox
            v-for="option in databaseOptions"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item
        v-if="selected.data_sources"
        :label="t('skill.dataSource')"
      >
        <el-radio-group v-model="form.data_source">
          <el-radio
            v-for="option in sourceOptions"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item
        v-for="(field, key) in selected.user_env_vars"
        :key="key"
        :label="field.label"
        :required="field.required"
      >
        <el-input
          v-model="form.user_env_vars[key]"
          type="password"
          show-password
          :placeholder="field.placeholder"
          autocomplete="new-password"
        />
      </el-form-item>
      <el-form-item
        v-if="selected.security_level === 'internal'"
        :label="t('skill.policy')"
      >
        <el-input
          v-model="form.requested_security_prompt"
          type="textarea"
          :rows="6"
          maxlength="16000"
        />
      </el-form-item>
      <el-checkbox v-model="form.reinstall_code">
        {{ t('skill.reinstall') }}
      </el-checkbox>
    </el-form>
    <template #footer>
      <el-button @click="editing = false">
        {{ t('common.cancel') }}
      </el-button><el-button
        type="primary"
        :loading="busy"
        data-test="skill-submit"
        @click="install"
      >
        {{ t('common.submit') }}
      </el-button>
    </template>
  </el-dialog>
</template>
