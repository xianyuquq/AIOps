import { createRouter, createWebHashHistory } from 'vue-router'
import { getToken } from './api'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue'), meta: { public: true } },
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: () => import('./views/Dashboard.vue') },
  { path: '/terminal', component: () => import('./views/Terminal.vue') },
  { path: '/servers', component: () => import('./views/Servers.vue') },
  { path: '/metrics', component: () => import('./views/Metrics.vue') },
  { path: '/scripts', component: () => import('./views/Scripts.vue') },
  { path: '/alerts', component: () => import('./views/Alerts.vue') },
  { path: '/ai', component: () => import('./views/AiChat.vue') },
  { path: '/databases', component: () => import('./views/Databases.vue') },
  { path: '/backups', component: () => import('./views/Backups.vue') },
  { path: '/audit', component: () => import('./views/Audit.vue') },
  { path: '/files', component: () => import('./views/Files.vue') }
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach((to) => {
  if (!to.meta.public && !getToken()) return '/login'
  if (to.path === '/login' && getToken()) return '/dashboard'
  return true
})

export default router
