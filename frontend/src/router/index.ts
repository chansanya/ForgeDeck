/* 定义业务路由、懒加载页面、登录守卫和浏览器标题。 */

import { createRouter, createWebHistory } from 'vue-router'
import { useSessionStore } from '@/stores/session'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      component: () => import('@/layouts/AppShell.vue'),
      children: [
        { path: '', name: 'dashboard', component: () => import('@/views/DashboardView.vue'), meta: { title: '总览' } },
        { path: 'projects', name: 'projects', component: () => import('@/views/ProjectsView.vue'), meta: { title: '项目' } },
        { path: 'pipelines', name: 'pipelines', component: () => import('@/views/PipelinesView.vue'), meta: { title: '流水线' } },
        { path: 'runs/:id', name: 'run-detail', component: () => import('@/views/RunDetailView.vue'), meta: { title: '运行详情' } },
        { path: 'servers', name: 'servers', component: () => import('@/views/ServersView.vue'), meta: { title: '服务器' } },
        { path: 'docker', name: 'docker', component: () => import('@/views/DockerView.vue'), meta: { title: 'Docker' } },
        { path: 'deployments', name: 'deployments', component: () => import('@/views/DeploymentsView.vue'), meta: { title: '部署' } },
        { path: 'scripts', name: 'scripts', component: () => import('@/views/ScriptsView.vue'), meta: { title: '脚本' } },
        { path: 'credentials', name: 'credentials', component: () => import('@/views/CredentialsView.vue'), meta: { title: '凭据' } },
        { path: 'approvals', name: 'approvals', component: () => import('@/views/ApprovalsView.vue'), meta: { title: '审批' } },
        { path: 'integrations', name: 'integrations', component: () => import('@/views/IntegrationsView.vue'), meta: { title: '通知与 MCP' } },
        { path: 'audit', name: 'audit', component: () => import('@/views/AuditView.vue'), meta: { title: '审计日志' } },
        { path: 'ssh/:serverId?', name: 'ssh', component: () => import('@/views/SshTerminalView.vue'), meta: { title: 'SSH 终端' } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  /** 在切换页面前恢复登录态并执行公开路由与标题策略。 */
  const session = useSessionStore()
  await session.hydrate()
  if (!to.meta.public && !session.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && session.isAuthenticated) return { name: 'dashboard' }
  document.title = `${String(to.meta.title || '控制台')} · ForgeDeck`
})

window.addEventListener('devops:unauthorized', () => {
  const session = useSessionStore()
  session.logout()
  if (router.currentRoute.value.name !== 'login') {
    void router.replace({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
  }
})

export default router
