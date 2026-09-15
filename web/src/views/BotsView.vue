<script setup lang="ts">
import { errorMessage } from '@/utils/errors'
import LoadState from '@/components/LoadState.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { bots, catalog, relays } from '@/api/admin'
import type { BotOut, CatalogOut, Platform, RelayOut } from '@/api/types'
import { usePaged } from '@/composables/usePaged'
import { useAuthStore } from '@/stores/auth'
import BotForm from '@/views/BotForm.vue'

const botFormRef = ref<InstanceType<typeof BotForm>>()
async function closeForm(done: () => void) { if (!botFormRef.value || await botFormRef.value.confirmDiscard()) done() }
const { t } = useI18n()
const auth = useAuthStore()
const router = useRouter()

const PLATFORMS: Platform[] = ['wecom', 'feishu']
const SCOPES = ['mine', 'team', 'all'] as const
type Scope = (typeof SCOPES)[number]

type BotFilters = {
  scope: Scope
  keyword: string
  platform: Platform | null
  enabled: boolean | null
  relay_server_id: string | null
  model: string | null
}

const paged = usePaged<BotOut, BotFilters>((q) => bots.list(q), {
  scope: 'mine',
  keyword: '',
  platform: null,
  enabled: null,
  relay_server_id: null,
  model: null,
})

const relayList = ref<RelayOut[]>([])
const catalogRows = ref<CatalogOut[]>([])
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingBot = ref<BotOut | null>(null)

// member 必须先有团队才能建机器人（后端 can_create_bot 同款判定）。
const canCreate = computed(() => auth.user?.role !== 'member' || !!auth.user?.team_id)
const hasTeam = computed(() => !!auth.user?.team_id)
const visibleScopes = computed(() => SCOPES.filter((s) => s !== 'team' || hasTeam.value))
const modelOptions = computed(() => [...new Set(catalogRows.value.map((r) => r.model))])

function fail(e: unknown): void {
  ElMessage.error(errorMessage(e))
}

function search(): void {
  paged.page.value = 1
  paged.load()
}

function resetFilters(): void {
  paged.reset()
  paged.load()
}

function onPageChange(p: number): void {
  paged.page.value = p
  paged.load()
}

function onSizeChange(size: number): void {
  paged.perPage.value = size
  paged.page.value = 1
  paged.load()
}

function openDetail(row: BotOut): void {
  router.push({ name: 'bot-detail', params: { id: row.id } })
}

/** 切换 relay 的对话框在详情页（Task 12），列表这里只负责带着 action 跳过去。 */
function openSwitchRelay(row: BotOut): void {
  router.push({ name: 'bot-detail', params: { id: row.id }, query: { action: 'switch' } })
}

async function toggle(row: BotOut): Promise<void> {
  try {
    Object.assign(row, await bots.toggle(row.id))
  } catch (e) {
    fail(e)
    // 开关是受控的（:model-value=row.enabled），失败后重新拉一次让它回到后端的真值。
    await paged.load()
  }
}

async function remove(row: BotOut): Promise<void> {
  try {
    await ElMessageBox.confirm(t('bots.deleteConfirm'), t('common.delete'), {
      type: 'warning',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    await bots.remove(row.id)
    ElMessage.success(t('common.deleted'))
    await paged.load()
  } catch (e) {
    fail(e)
  }
}

function openCreate(): void {
  dialogMode.value = 'create'
  editingBot.value = null
  dialogVisible.value = true
}

async function openEdit(row: BotOut): Promise<void> {
  // 列表用 include_sensitive=False，提示词/凭证/环境变量都不在里面，编辑前先取完整对象。
  try {
    editingBot.value = await bots.get(row.id)
    dialogMode.value = 'edit'
    dialogVisible.value = true
  } catch (e) {
    fail(e)
  }
}

async function onSaved(): Promise<void> {
  dialogVisible.value = false
  await paged.load()
}

onMounted(async () => {
  await paged.load()
  try {
    const [relayPage, rows] = await Promise.all([relays.list({ is_active: true, per_page: 200 }), catalog.list()])
    relayList.value = relayPage.items
    catalogRows.value = rows
  } catch (e) {
    fail(e)
  }
})
</script>

<template>
  <div class="bots-view">
    <div class="page-header cm-page-header">
      <h2>{{ t('bots.title') }}</h2>
      <p class="cm-page-intro">
        {{ t('workspace.intro.bots') }}
      </p>
      <el-tooltip
        :disabled="canCreate"
        :content="t('bots.needTeam')"
        placement="top"
      >
        <span>
          <el-button
            type="primary"
            :disabled="!canCreate"
            data-test="create-bot"
            @click="openCreate"
          >
            {{ t('bots.create') }}
          </el-button>
        </span>
      </el-tooltip>
    </div>

    <el-radio-group
      v-model="paged.filters.scope"
      class="scope"
      @change="search"
    >
      <el-radio-button
        v-for="s in visibleScopes"
        :key="s"
        :data-test="'scope-' + s"
        :value="s"
      >
        {{ t(`bots.scope.${s}`) }}
      </el-radio-button>
    </el-radio-group>

    <el-form
      :inline="true"
      @submit.prevent
    >
      <el-form-item :label="t('bots.keyword')">
        <el-input
          v-model="paged.filters.keyword"
          clearable
          style="width: 180px"
          @keyup.enter="search"
        />
      </el-form-item>
      <el-form-item :label="t('bots.platform')">
        <el-select
          v-model="paged.filters.platform"
          clearable
          :placeholder="t('bots.platform')"
          style="width: 120px"
        >
          <el-option
            v-for="p in PLATFORMS"
            :key="p"
            :label="t(`platforms.${p}`)"
            :value="p"
          />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('bots.status')">
        <el-select
          v-model="paged.filters.enabled"
          clearable
          :placeholder="t('bots.status')"
          style="width: 120px"
        >
          <el-option
            :label="t('common.enabled')"
            :value="true"
          />
          <el-option
            :label="t('common.disabled')"
            :value="false"
          />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('bots.relay')">
        <el-select
          v-model="paged.filters.relay_server_id"
          clearable
          :placeholder="t('bots.relay')"
          style="width: 180px"
        >
          <el-option
            v-for="r in relayList"
            :key="r.id"
            :label="r.name"
            :value="r.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('bots.model')">
        <el-select
          v-model="paged.filters.model"
          clearable
          filterable
          :placeholder="t('bots.model')"
          style="width: 220px"
        >
          <el-option
            v-for="m in modelOptions"
            :key="m"
            :label="m"
            :value="m"
          />
        </el-select>
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
        min-width="200"
        :label="t('bots.name')"
      >
        <template #default="{ row }: { row: BotOut }">
          <el-button
            link
            type="primary"
            :data-test="'row-' + row.id"
            :title="t('bots.detail.view')"
            @click="openDetail(row)"
          >
            {{ row.name }}
          </el-button>
          <div class="muted">
            {{ row.bot_key }}
          </div>
        </template>
      </el-table-column>
      <el-table-column
        :label="t('bots.platform')"
        width="100"
      >
        <template #default="{ row }: { row: BotOut }">
          <el-tag size="small">
            {{ t(`platforms.${row.platform}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('bots.relay')"
      >
        <template #default="{ row }: { row: BotOut }">
          {{ row.relay_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('bots.model')"
      >
        <template #default="{ row }: { row: BotOut }">
          <div>{{ row.model }}</div>
          <el-tag
            size="small"
            type="info"
          >
            {{ t(`bots.backend.${row.backend}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('bots.team')"
      >
        <template #default="{ row }: { row: BotOut }">
          {{ row.team_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        min-width="140"
        :label="t('bots.creator')"
      >
        <template #default="{ row }: { row: BotOut }">
          {{ row.created_by_name ?? '—' }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('bots.status')"
        width="90"
      >
        <template #default="{ row }: { row: BotOut }">
          <el-switch
            :data-test="'toggle-' + row.id"
            :model-value="row.enabled"
            :disabled="!row.permissions.can_toggle"
            @change="toggle(row)"
          />
        </template>
      </el-table-column>
      <el-table-column
        :label="t('common.actions')"
        width="240"
      >
        <template #default="{ row }: { row: BotOut }">
          <el-button
            v-if="row.permissions.can_edit"
            size="small"
            :data-test="'edit-' + row.id"
            @click="openEdit(row)"
          >
            {{ t('bots.edit') }}
          </el-button>
          <el-button
            v-if="row.permissions.can_switch_relay"
            size="small"
            :data-test="'switch-' + row.id"
            @click="openSwitchRelay(row)"
          >
            {{ t('bots.switchRelay') }}
          </el-button>
          <el-button
            v-if="row.permissions.can_delete"
            size="small"
            type="danger"
            :data-test="'delete-' + row.id"
            @click="remove(row)"
          >
            {{ t('bots.delete') }}
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

    <el-dialog
      v-model="dialogVisible"
      :close-on-click-modal="false"
      class="employee-dialog"
      :before-close="closeForm"
      :title="dialogMode === 'edit' ? t('bots.edit') : t('bots.create')"
      width="760px"
      destroy-on-close
    >
      <!-- 只在对话框开着时才实例化：BotForm 挂载时会去拉 settings/teams/relays/catalog。 -->
      <BotForm
        v-if="dialogVisible"
        ref="botFormRef"
        :mode="dialogMode"
        :bot="editingBot ?? undefined"
        @saved="onSaved"
        @cancel="dialogVisible = false"
      />
    </el-dialog>
  </div>
</template>

<style scoped>
.scope { margin-bottom: 12px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
