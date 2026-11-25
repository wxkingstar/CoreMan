import type { RouteLocationNormalizedLoaded } from 'vue-router'

/**
 * 管理台布局里 `<router-view>` 的 key：默认按路径，换地址就重建页面（例如从一个员工切到另一个）。
 * 路由声明了 `meta.viewKey` 的，同一个 key 下只换参数、不重建（任务页打开详情不刷新列表）。
 */
export function viewKey(route: RouteLocationNormalizedLoaded): string {
  return route.meta.viewKey ?? route.path
}
