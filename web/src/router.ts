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

export default router
