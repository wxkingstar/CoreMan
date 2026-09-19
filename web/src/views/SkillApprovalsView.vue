<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { useListQuery } from '@/composables/useListQuery'
import { computed, onMounted, ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { skills, allSkills, type Skill, type Approval } from '@/api/skills'
const { t } = useI18n()
const rows = ref<Approval[]>([]), catalog = ref<Skill[]>([]), selected = ref<Approval | null>(null)
const page = ref(1), total = ref(0), busy = ref(false), visible = ref(false)
const staleApproval = computed(() => { const row = selected.value; const current = catalog.value.find(skill => skill.id === row?.skill_id); return !!row && !!current && row.skill_revision != null && current.revision !== row.skill_revision })
const form = reactive({ decision: 'approve' as 'approve' | 'reject', approved_databases: [] as string[], approved_security_prompt: '', comment: '' })
const fail = (e: unknown) => ElMessage.error(errorMessage(e))
const listLoading = ref(false), listError = ref('')
const { persist: persistQuery } = useListQuery({ page }, () => { void load() })
async function load() {
  persistQuery(); listLoading.value = true; listError.value = ''
 try { const data = await skills.approvals(page.value); rows.value = data.items; total.value = data.total } catch (e) { listError.value = errorMessage(e); fail(e) }  finally { listLoading.value = false }
}
function edit(row: Approval) { selected.value = row; Object.assign(form, { decision: 'approve', approved_databases: [...row.requested_databases], approved_security_prompt: row.requested_security_prompt, comment: '' }); visible.value = true }
async function save() { if (!selected.value || busy.value || staleApproval.value && form.decision === 'approve') return; busy.value = true; try { await skills.review(selected.value, form); visible.value = false; await load() } catch (e) { fail(e) } finally { busy.value = false } }
onMounted(async () => { await load(); try { catalog.value = await allSkills() } catch (e) { fail(e) } })
</script>
<template>
  <section>
    <header class="cm-page-header">
      <h2>{{ t('menu.skillApprovals') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.skill-approvals') }}
      </p><el-button @click="load">
        {{ t('common.refresh') }}
      </el-button>
    </header>
    <LoadState
      :loading="listLoading"
      :error="listError"
      @retry="load"
    />
    <el-table :data="rows">
      <el-table-column
        min-width="140"
        :label="t('menu.skills')"
      >
        <template #default="{ row }">
          {{ row.skill_name ?? catalog.find(s => s.id === row.skill_id)?.name ?? row.skill_id }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('menu.bots')"
      >
        <template #default="{ row }">
          <router-link :to="`/bots/${row.bot_id}`">
            {{ row.bot_name || '—' }}
          </router-link>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        prop="requested_by"
        :label="t('skill.requester')"
      />
      <el-table-column
        min-width="140"
        :label="t('common.status')"
      >
        <template #default="{ row }">
          {{ t(`skill.status.${row.status}`) }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        prop="review_comment"
        :label="t('skill.comment')"
      />
      <el-table-column min-width="140">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'pending'"
            @click="edit(row)"
          >
            {{ t('skill.review') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="page"
      :page-size="50"
      :total="total"
      layout="prev, pager, next"
      @current-change="load"
    />
    <el-dialog
      v-model="visible"
      :close-on-click-modal="false"
      :title="t('skill.review')"
      width="680px"
    >
      <el-alert
        v-if="staleApproval"
        :title="t('workspace.staleApproval')"
        type="warning"
        :closable="false"
      />
      <el-descriptions
        :column="1"
        border
      >
        <el-descriptions-item :label="t('workspace.revision')">
          {{ selected?.skill_revision ?? t('workspace.unknown') }}
        </el-descriptions-item><el-descriptions-item :label="t('skill.databases')">
          {{ selected?.requested_databases.join(', ') || '—' }}
        </el-descriptions-item>
      </el-descriptions>
      <el-form label-position="top">
        <el-form-item :label="t('skill.decision')">
          <el-radio-group v-model="form.decision">
            <el-radio value="approve">
              {{ t('skill.approve') }}
            </el-radio><el-radio value="reject">
              {{ t('skill.reject') }}
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="t('skill.databases')">
          <el-checkbox-group v-model="form.approved_databases">
            <el-checkbox
              v-for="key in selected?.requested_databases"
              :key="key"
              :value="key"
            >
              {{ key }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item :label="t('skill.requestedPolicy')">
          <p class="policy">
            {{ selected?.requested_security_prompt }}
          </p>
        </el-form-item>
        <el-form-item :label="t('skill.policy')">
          <el-input
            v-model="form.approved_security_prompt"
            type="textarea"
            :rows="6"
          />
        </el-form-item>
        <el-form-item :label="t('skill.comment')">
          <el-input
            v-model="form.comment"
            maxlength="2000"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="visible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          :loading="busy"
          :disabled="staleApproval && form.decision === 'approve'"
          @click="save"
        >
          {{ t('common.submit') }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
<style scoped>.policy { white-space: pre-wrap; overflow-wrap: anywhere; }</style>
