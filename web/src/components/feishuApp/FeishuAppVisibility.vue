<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { departments } from '@/api/admin'
import { feishuApps, type FeishuAppOverview } from '@/api/feishuApps'
import type { DeptNode } from '@/api/types'
import UserPicker from '@/components/UserPicker.vue'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ botId: string; overview: FeishuAppOverview; disabled?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const mode = ref<'all' | 'some'>('all')
const userIds = ref<string[]>([])
const departmentIds = ref<string[]>([])
const tree = ref<DeptNode[]>([])
const saving = ref(false)

const requested = computed(() => (props.overview.versions.under_review ?? props.overview.versions.online)?.visibility ?? null)

async function save(): Promise<void> {
  if (mode.value === 'some' && !userIds.value.length && !departmentIds.value.length) {
    ElMessage.error(t('feishuApp.visibilityRequired'))
    return
  }
  saving.value = true
  try {
    await feishuApps.updateVisibility(props.botId, {
      visible_to_all: mode.value === 'all',
      user_ids: mode.value === 'all' ? [] : userIds.value,
      department_ids: mode.value === 'all' ? [] : departmentIds.value,
    })
    ElMessage.success(t('feishuApp.savedPublish'))
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    tree.value = await departments.tree('feishu')
  } catch {
    tree.value = [] // 没同步飞书通讯录时只按成员选择
  }
})
</script>

<template>
  <section
    class="cm-panel"
    data-test="feishu-app-visibility"
  >
    <h3>{{ t('feishuApp.visibility') }}</h3>
    <p class="cm-page-intro">
      {{ t('feishuApp.visibilityHint') }}
    </p>
    <p
      v-if="requested"
      class="muted"
    >
      {{ requested.is_all ? t('feishuApp.visibilityCurrentAll') : t('feishuApp.visibilityCurrentSome', { users: requested.open_ids.length, departments: requested.department_ids.length }) }}
    </p>
    <el-form
      label-width="120px"
      :disabled="disabled"
      @submit.prevent
    >
      <el-form-item :label="t('feishuApp.visibilityScope')">
        <el-radio-group
          v-model="mode"
          data-test="visibility-mode"
        >
          <el-radio value="all">
            {{ t('feishuApp.visibilityAll') }}
          </el-radio>
          <el-radio value="some">
            {{ t('feishuApp.visibilitySome') }}
          </el-radio>
        </el-radio-group>
      </el-form-item>
      <template v-if="mode === 'some'">
        <el-form-item :label="t('feishuApp.visibilityUsers')">
          <UserPicker v-model="userIds" />
        </el-form-item>
        <el-form-item :label="t('feishuApp.visibilityDepartments')">
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
      </template>
      <el-form-item>
        <el-button
          type="primary"
          :loading="saving"
          data-test="visibility-save"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </el-form-item>
    </el-form>
  </section>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
</style>
