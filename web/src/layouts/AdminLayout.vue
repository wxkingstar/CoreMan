<script lang="ts">
import type { Role } from '@/api/types'

export interface MenuItem {
  key: string
  path: string
  /** 不填 = 所有登录用户可见；填了就只对这些角色显示（后端路由另有一套同样的守卫）。 */
  roles?: readonly Role[]
}

// 已开放的菜单项。`<script setup>` 里不能 export，所以放在这个普通 script 块中，
// 给 AdminLayout.spec 与后续任务复用同一份角色可见性定义。
export const MENU: readonly MenuItem[] = [
  { key: 'statistics', path: '/statistics' },
  { key: 'bots', path: '/bots' },
  { key: 'relays', path: '/runtimes' },
  { key: 'skills', path: '/skills' },
  { key: 'skillApprovals', path: '/skill-approvals', roles: ['ai_committee', 'platform_admin'] },
  { key: 'cron', path: '/cron' },
  { key: 'chatLogs', path: '/chat-logs' },
  { key: 'announcements', path: '/announcements', roles: ['ai_committee', 'platform_admin'] },
  { key: 'runtime', path: '/runtime', roles: ['team_lead', 'ai_committee', 'platform_admin'] },
  { key: 'users', path: '/users' },
  { key: 'apps', path: '/apps', roles: ['platform_admin'] },
  { key: 'audit', path: '/audit', roles: ['ai_committee', 'platform_admin'] },
  { key: 'systems', path: '/systems', roles: ['ai_committee', 'platform_admin'] },
  { key: 'credentials', path: '/credentials', roles: ['platform_admin'] },
  { key: 'settings', path: '/settings', roles: ['platform_admin'] },
]
</script>


<script setup lang="ts">
import BrandLogo from '@/components/BrandLogo.vue'
import { computed, ref, watch } from 'vue'
import { Menu as MenuIcon, Close, Grid, User, Collection, Timer, ChatDotRound, TrendCharts, Checked, Bell, Monitor, Setting, Connection, Document, Key, OfficeBuilding, SwitchButton, Moon } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { getLocale, setLocale, type Locale } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
const { t } = useI18n()
const auth = useAuthStore(), router = useRouter(), route = useRoute()
const locale = ref<Locale>(getLocale())
const dark = ref(document.documentElement.classList.contains('dark'))
const mobileOpen = ref(false)
const icons: Record<string, typeof Grid> = { bots: User, skills: Collection, cron: Timer, chatLogs: ChatDotRound, statistics: TrendCharts, skillApprovals: Checked, announcements: Bell, relays: Connection, runtime: Monitor, users: User, apps: Connection, audit: Document, systems: OfficeBuilding, credentials: Key, settings: Setting }
const groups = [
  { key: 'collaboration', keys: ['bots', 'skills', 'cron', 'chatLogs'] },
  { key: 'governance', keys: ['statistics', 'skillApprovals', 'announcements', 'audit'] },
  { key: 'platform', keys: ['relays', 'runtime', 'users', 'apps', 'systems', 'credentials', 'settings'] },
]
const organizationKeys = ['users', 'apps', 'systems', 'credentials']
function visible(item: MenuItem): boolean { return !item.roles || !!auth.user && item.roles.includes(auth.user.role as Role) }
const visibleGroups = computed(() => groups.map(group => ({ ...group, items: group.keys.map(key => MENU.find(item => item.key === key)!).filter(visible) })).filter(group => group.items.length))
const activePath = computed(() => route.path.startsWith('/bots/') ? '/bots' : route.path)
const title = computed(() => t('menu.' + (MENU.find(item => item.path === activePath.value)?.key ?? 'home')))
watch(() => route.path, () => { mobileOpen.value = false })
function toggleDark() { dark.value = !dark.value; document.documentElement.classList.toggle('dark', dark.value); try { localStorage.setItem('coreman.dark', dark.value ? '1' : '0') } catch { /* unavailable storage */ } }
async function logout() { await auth.logout(); await router.push({ name: 'login' }) }
</script>
<template>
  <el-container class="layout">
    <a
      class="skip-link"
      href="#main-content"
    >{{ t('workspace.skip') }}</a>
    <el-aside
      width="220px"
      class="aside desktop-nav"
    >
      <router-link
        class="brand"
        to="/"
      >
        <BrandLogo :size="36" /><span>CoreMan<small>{{ t('app.subtitle') }}</small></span>
      </router-link>
      <el-menu
        router
        :default-active="activePath"
        :default-openeds="['organization']"
      >
        <el-menu-item index="/">
          <el-icon><Grid /></el-icon><span>{{ t('menu.home') }}</span>
        </el-menu-item>
        <el-menu-item-group
          v-for="group in visibleGroups"
          :key="group.key"
          :title="t('workspace.' + group.key)"
        >
          <template
            v-for="item in group.items"
            :key="item.key"
          >
            <el-sub-menu
              v-if="item.key === 'users'"
              index="organization"
            >
              <template #title>
                <el-icon><OfficeBuilding /></el-icon><span>{{ t('workspace.organization') }}</span>
              </template>
              <el-menu-item
                v-for="child in group.items.filter(row => organizationKeys.includes(row.key))"
                :key="child.key"
                :index="child.path"
              >
                {{ t('menu.' + child.key) }}
              </el-menu-item>
            </el-sub-menu>
            <el-menu-item
              v-else-if="!organizationKeys.includes(item.key)"
              :index="item.path"
            >
              <el-icon><component :is="icons[item.key]" /></el-icon><span>{{ t('menu.' + item.key) }}</span>
            </el-menu-item>
          </template>
        </el-menu-item-group>
      </el-menu>
      <div class="aside-footer">
        {{ t('workspace.scope') }}<br><strong>{{ auth.user?.display_name }}</strong>
      </div>
    </el-aside>
    <el-container class="main-shell">
      <el-header class="header">
        <div class="breadcrumb">
          <el-button
            class="mobile-toggle"
            :icon="MenuIcon"
            :aria-label="t('workspace.navigation')"
            @click="mobileOpen = true"
          /><span>{{ title }}</span>
        </div>
        <div class="controls">
          <el-select
            v-model="locale"
            :aria-label="t('workspace.language')"
            style="width: 105px"
            @change="setLocale"
          >
            <el-option
              label="中文"
              value="zh"
            /><el-option
              label="日本語"
              value="ja"
            /><el-option
              label="English"
              value="en"
            />
          </el-select>
          <el-button
            :icon="Moon"
            :aria-label="t('workspace.theme')"
            :aria-pressed="dark"
            @click="toggleDark"
          />
          <span class="user-name">{{ auth.user?.display_name }}</span>
          <el-button
            :icon="SwitchButton"
            :aria-label="t('layout.logout')"
            @click="logout"
          />
        </div>
      </el-header>
      <el-main
        id="main-content"
        tabindex="-1"
      >
        <router-view :key="route.path" />
      </el-main>
    </el-container>
    <el-drawer
      v-model="mobileOpen"
      direction="ltr"
      size="280px"
      :title="t('app.title')"
      class="mobile-navigation"
    >
      <nav :aria-label="t('workspace.navigation')">
        <router-link
          class="mobile-link"
          to="/"
        >
          {{ t('menu.home') }}
        </router-link>
        <section
          v-for="group in visibleGroups"
          :key="group.key"
        >
          <h3>{{ t('workspace.' + group.key) }}</h3><router-link
            v-for="item in group.items"
            :key="item.key"
            :to="item.path"
            class="mobile-link"
            :aria-current="activePath === item.path ? 'page' : undefined"
          >
            {{ t('menu.' + item.key) }}
          </router-link>
        </section>
      </nav>
      <template #footer>
        <el-button
          :icon="Close"
          @click="mobileOpen = false"
        >
          {{ t('workspace.closeNavigation') }}
        </el-button>
      </template>
    </el-drawer>
  </el-container>
</template>
<style scoped>
.layout { min-height: 100vh; }
.aside { position: sticky; top: 0; height: 100vh; overflow-y: auto; border-right: 1px solid var(--cm-border); background: var(--cm-surface); padding: 24px 12px 12px; flex-shrink: 0; }
.brand { display: flex; align-items: center; gap: 12px; padding: 0 12px 28px; color: var(--cm-text); font-size: 21px; font-weight: 700; text-decoration: none; }
.brand small { display: block; font-size: 11px; font-weight: 400; color: var(--cm-muted); letter-spacing: 1px; }
.el-menu { border: 0; background: transparent; }
:deep(.el-menu-item), :deep(.el-sub-menu__title) { height: 44px; border-radius: 7px; padding-left: 12px !important; margin-bottom: 3px; }
:deep(.el-menu-item.is-active) { background: var(--cm-brand-soft); font-weight: 600; }
:deep(.el-menu-item-group__title) { padding: 22px 12px 10px; font-size: 11px; letter-spacing: 1px; color: var(--cm-muted); }
:deep(.el-sub-menu .el-menu-item) { min-width: 0; padding-left: 42px !important; font-size: 13px; }
.aside-footer { border-top: 1px solid var(--cm-border); padding: 16px 12px 4px; margin-top: 24px; color: var(--cm-muted); font-size: 12px; }
.main-shell { min-width: 0; display: flex; flex-direction: column; }
.header { display: flex; align-items: center; justify-content: space-between; gap: 12px; height: 72px; padding: 0 28px; border-bottom: 1px solid var(--cm-border); background: var(--cm-surface); }
.controls, .breadcrumb { display: flex; gap: 12px; align-items: center; min-width: 0; }
.controls .el-button { margin: 0; } .user-name { color: var(--cm-muted); font-size: 13px; }
.breadcrumb { font-weight: 600; }
.mobile-toggle { display: none; }
.mobile-link { display: block; padding: 10px 12px; border-radius: 7px; color: var(--cm-text); }
.mobile-link[aria-current=page] { background: var(--cm-brand-soft); color: var(--el-color-primary); }
.mobile-navigation h3 { margin: 24px 12px 8px; font-size: 12px; color: var(--cm-muted); }
.skip-link { position: fixed; top: -100px; left: 16px; z-index: 3000; padding: 10px 18px; background: var(--cm-surface); }.skip-link:focus { top: 10px; }
@media(max-width:1023px) { .desktop-nav { display: none; } .mobile-toggle { display: inline-flex; } .header { padding: 0 20px; } }
@media(max-width:600px) { .header { padding: 0 12px; height: 64px; } .controls { gap: 6px; } .user-name, .breadcrumb > span { display: none; } }
</style>
