import { createRouter, createWebHistory } from 'vue-router'
import { useSessionStore } from './stores/session'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/projects' },
    { path: '/projects', component: () => import('./pages/ProjectsPage.vue') },
    {
      path: '/names', component: () => import('./pages/NamesPage.vue'),
      meta: { needsProject: true },
    },
    {
      path: '/strings', component: () => import('./pages/TextsPage.vue'),
      props: { contentType: 'ui' }, meta: { needsProject: true },
    },
    {
      path: '/dialogue', component: () => import('./pages/TextsPage.vue'),
      props: { contentType: 'dialogue' }, meta: { needsProject: true },
    },
    {
      path: '/export', component: () => import('./pages/ExportPage.vue'),
      meta: { needsProject: true },
    },
    { path: '/settings', component: () => import('./pages/SettingsPage.vue') },
  ],
})

// 无打开项目时，项目相关页面跳回项目管理
router.beforeEach((to) => {
  const session = useSessionStore()
  if (to.meta.needsProject && session.loaded && !session.currentProject) {
    return '/projects'
  }
  return true
})

// 前端重新构建后，旧页面里未访问过的路由 chunk（旧 hash）已不存在，
// 动态 import 404 会导致点击菜单无反应。检测到 chunk 加载失败时
// 整页刷新，拿到与新构建一致的 index.html 和 chunk。
router.onError((error) => {
  const msg = String(error?.message || error)
  if (msg.includes('dynamically imported module')
      || msg.includes('Importing a module script failed')
      || msg.includes('error loading dynamically imported module')
      || msg.includes('Failed to fetch')) {
    window.location.reload()
  }
})

export default router
