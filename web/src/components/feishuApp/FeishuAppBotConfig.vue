<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { feishuApps, type FeishuAppOverview, type FeishuMenu, type FeishuMenuKind } from '@/api/feishuApps'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ botId: string; overview: FeishuAppOverview; disabled?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const KINDS: FeishuMenuKind[] = ['link', 'submenu', 'message', 'event']
const STRATEGIES = [1, 2, 3] as const

const form = reactive<{ get_started_desc: string; menu_enabled: boolean; menu_display_strategy: 1 | 2 | 3; menus: FeishuMenu[] }>({
  get_started_desc: '', menu_enabled: false, menu_display_strategy: 1, menus: [],
})
const saving = ref(false)
let seq = 0

watch(() => props.overview, (value) => {
  // 审核中的版本比线上版本新：优先展示它，避免覆盖别人刚提交的菜单。
  const bot = value.versions.under_review?.bot ?? value.versions.online?.bot
  form.menu_enabled = bot?.menu_enabled ?? false
  form.menu_display_strategy = ([1, 2, 3] as const).find(s => s === bot?.menu_display_strategy) ?? 1
  form.menus = (bot?.menus ?? []).map(menu => ({ ...menu }))
}, { immediate: true })

const parents = computed(() => form.menus.filter(menu => menu.kind === 'submenu' && !menu.parent_menu_id))

function addMenu(): void {
  form.menus.push({ menu_id: `m${Date.now().toString(36)}${seq++}`, parent_menu_id: null, name: '', kind: 'link', pc_url: null, mobile_url: null, event_key: null, sort: form.menus.length })
}

function removeMenu(index: number): void {
  const [removed] = form.menus.splice(index, 1)
  // 删掉上级时一并删掉其子菜单，免得留下悬空节点。
  if (removed?.kind === 'submenu') form.menus = form.menus.filter(menu => menu.parent_menu_id !== removed.menu_id)
}

function onKindChange(menu: FeishuMenu): void {
  if (menu.kind === 'submenu') {
    menu.parent_menu_id = null
    form.menus.forEach(child => { if (child.parent_menu_id === menu.menu_id && child.kind === 'submenu') child.parent_menu_id = null })
  } else {
    form.menus.forEach(child => { if (child.parent_menu_id === menu.menu_id) child.parent_menu_id = null })
  }
}

function invalid(): string | null {
  if (form.menu_enabled && !form.menus.length) return t('feishuApp.menuEmpty')
  for (const menu of form.menus) {
    if (!menu.name.trim()) return t('feishuApp.menuNameRequired')
    if (menu.kind === 'link' && !/^https:\/\//.test(menu.pc_url ?? '')) return t('feishuApp.menuLinkRequired')
    if (menu.kind === 'event' && !menu.event_key) return t('feishuApp.menuEventRequired')
  }
  return null
}

async function save(): Promise<void> {
  const problem = invalid()
  if (problem) {
    ElMessage.error(problem)
    return
  }
  saving.value = true
  try {
    await feishuApps.updateBot(props.botId, {
      language: props.overview.app?.primary_language ?? 'zh_cn',
      get_started_desc: form.get_started_desc.trim() || null,
      menu_enabled: form.menu_enabled,
      menu_display_strategy: form.menu_display_strategy,
      menus: form.menus.map((menu, index) => ({
        ...menu,
        name: menu.name.trim(),
        sort: index,
        pc_url: menu.kind === 'link' ? menu.pc_url : null,
        mobile_url: menu.kind === 'link' ? menu.pc_url : null,
        event_key: menu.kind === 'event' ? menu.event_key : null,
      })),
    })
    ElMessage.success(t('feishuApp.savedPublish'))
    emit('changed')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section
    class="cm-panel"
    data-test="feishu-app-bot"
  >
    <h3>{{ t('feishuApp.botConfig') }}</h3>
    <el-form
      label-width="120px"
      :disabled="disabled"
      @submit.prevent
    >
      <el-form-item :label="t('feishuApp.getStarted')">
        <el-input
          v-model="form.get_started_desc"
          maxlength="64"
          :placeholder="t('feishuApp.getStartedPlaceholder')"
        />
      </el-form-item>
      <el-form-item :label="t('feishuApp.menuEnabled')">
        <el-switch
          v-model="form.menu_enabled"
          data-test="menu-enabled"
        />
        <el-select
          v-if="form.menu_enabled"
          v-model="form.menu_display_strategy"
          style="width: 220px; margin-left: 12px"
        >
          <el-option
            v-for="s in STRATEGIES"
            :key="s"
            :value="s"
            :label="t(`feishuApp.menuStrategies.${s}`)"
          />
        </el-select>
        <div class="muted">
          {{ t('feishuApp.menuHint') }}
        </div>
      </el-form-item>
      <template v-if="form.menu_enabled">
        <el-table
          :data="form.menus"
          size="small"
          data-test="menu-table"
        >
          <el-table-column
            :label="t('feishuApp.menuName')"
            min-width="140"
          >
            <template #default="{ row }">
              <el-input
                v-model="row.name"
                maxlength="60"
              />
            </template>
          </el-table-column>
          <el-table-column
            :label="t('feishuApp.menuKind')"
            width="150"
          >
            <template #default="{ row }">
              <el-select
                v-model="row.kind"
                @change="onKindChange(row)"
              >
                <el-option
                  v-for="kind in KINDS"
                  :key="kind"
                  :value="kind"
                  :label="t(`feishuApp.menuKinds.${kind}`)"
                />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('feishuApp.menuParent')"
            width="150"
          >
            <template #default="{ row }">
              <el-select
                v-model="row.parent_menu_id"
                clearable
                :disabled="row.kind === 'submenu'"
                :placeholder="t('feishuApp.menuTopLevel')"
              >
                <el-option
                  v-for="parent in parents.filter(p => p.menu_id !== row.menu_id)"
                  :key="parent.menu_id"
                  :value="parent.menu_id"
                  :label="parent.name || parent.menu_id"
                />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column
            :label="t('feishuApp.menuTarget')"
            min-width="200"
          >
            <template #default="{ row }">
              <el-input
                v-if="row.kind === 'link'"
                v-model="row.pc_url"
                placeholder="https://"
              />
              <el-input
                v-else-if="row.kind === 'event'"
                v-model="row.event_key"
                maxlength="30"
                :placeholder="t('feishuApp.menuEventKey')"
              />
              <span
                v-else
                class="muted"
              >{{ t(`feishuApp.menuKindHints.${row.kind}`) }}</span>
            </template>
          </el-table-column>
          <el-table-column width="70">
            <template #default="{ $index }">
              <el-button
                text
                type="danger"
                @click="removeMenu($index)"
              >
                {{ t('common.delete') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-button
          class="add"
          data-test="menu-add"
          @click="addMenu"
        >
          {{ t('feishuApp.menuAdd') }}
        </el-button>
      </template>
      <el-form-item class="save">
        <el-button
          type="primary"
          :loading="saving"
          data-test="bot-save"
          @click="save"
        >
          {{ t('common.save') }}
        </el-button>
      </el-form-item>
    </el-form>
  </section>
</template>

<style scoped>
.muted { width: 100%; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.add { margin: 8px 0 16px; }
.save { margin-top: 12px; }
</style>
