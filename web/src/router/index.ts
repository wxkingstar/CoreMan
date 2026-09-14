import { ElMessage } from 'element-plus'
import { createRouter, createWebHistory } from 'vue-router'
import { setUnauthorizedHandler } from '@/api/client'
import { i18n } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    roles?: string[]
  }
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
    {
      path: '/',
      component: () => import('@/layouts/AdminLayout.vue'),
      children: [
        { path: ':pathMatch(.*)*', name: 'not-found', component: () => import('@/views/NotFoundView.vue') },
        { path: '', name: 'home', component: () => import('@/views/HomeView.vue') },
        { path: 'statistics', name: 'statistics', component: () => import('@/views/StatisticsView.vue') },
        { path: 'systems', name: 'systems', component: () => import('@/views/SystemsView.vue'), meta: { roles: ['ai_committee', 'platform_admin'] } },
        { path: 'credentials', name: 'credentials', component: () => import('@/views/CredentialsView.vue'), meta: { roles: ['platform_admin'] } },
        { path: 'users', name: 'users', component: () => import('@/views/UsersView.vue') },
        { path: 'bots', name: 'bots', component: () => import('@/views/BotsView.vue') },
        { path: 'bots/:id', name: 'bot-detail', component: () => import('@/views/BotDetailView.vue') },
        { path: 'relays', redirect: '/runtimes' },
        { path: 'runtimes', name: 'relays', component: () => import('@/views/RuntimeNodesView.vue') },
        { path: 'skills', name: 'skills', component: () => import('@/views/SkillsView.vue') },
        { path: 'skill-approvals', name: 'skill-approvals', component: () => import('@/views/SkillApprovalsView.vue'), meta: { roles: ['ai_committee', 'platform_admin'] } },
        { path: 'cron', name: 'cron', component: () => import('@/views/CronView.vue') },
        { path: 'chat-logs', name: 'chat-logs', component: () => import('@/views/ChatLogsView.vue') },
        {
          path: 'announcements',
          name: 'announcements',
          component: () => import('@/views/AnnouncementsView.vue'),
          meta: { roles: ['ai_committee', 'platform_admin'] },
        },
        {
          path: 'runtime',
          name: 'runtime',
          component: () => import('@/views/RuntimeView.vue'),
          meta: { roles: ['team_lead', 'ai_committee', 'platform_admin'] },
        },
        {
          path: 'apps',
          name: 'apps',
          component: () => import('@/views/PlatformAppsView.vue'),
          meta: { roles: ['platform_admin'] },
        },
        {
          path: 'audit',
          name: 'audit',
          component: () => import('@/views/AuditLogsView.vue'),
          meta: { roles: ['ai_committee', 'platform_admin'] },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/SettingsView.vue'),
          meta: { roles: ['platform_admin'] },
        },
      ],
    },
  ],
})

setUnauthorizedHandler(() => {
  const auth = useAuthStore()
  // 初始 fetchMe 尚未完成时（auth.loaded === false），这里收到的 401 是 beforeEach 守卫探测登录态
  // 触发的，此时对应的导航还没提交（currentRoute 可能仍是 START_LOCATION），若在此处抢先 push 登录页，
  // 会用错误的 redirect 冲掉守卫本该完成的导航（例如丢失 /login?error=... 上的 error 参数，或深链接
  // 的目标路径）。守卫自身已经会在 fetchMe 结束后根据登录态跳转登录页并带上正确的 redirect，
  // 所以这种情况下必须保持 no-op。
  if (!auth.loaded) return
  const cur = router.currentRoute.value
  if (cur.name !== 'login') {
    auth.user = null
    router.push({ name: 'login', query: { redirect: cur.fullPath } })
  }
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) {
    try {
      await auth.fetchMe()
    } catch (e) {
      console.error('fetchMe failed, treating as logged out', e)
      auth.user = null
      auth.loaded = true
    }
  }
  if (!to.meta.public && !auth.user) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.name === 'login' && auth.user) return { name: 'home' }
  if (to.meta.roles && auth.user && !to.meta.roles.includes(auth.user.role)) {
    ElMessage.warning(i18n.global.t('common.noPermissionPage'))
    return { name: 'home' }
  }
  return true
})
