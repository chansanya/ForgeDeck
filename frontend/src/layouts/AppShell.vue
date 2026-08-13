<!-- 登录后主框架：组织响应式导航、顶栏、会话操作和路由内容区。 -->
<template>
  <NLayout class="shell" has-sider>
    <NLayoutSider
      v-if="!compact"
      bordered
      collapse-mode="width"
      :collapsed-width="74"
      :width="244"
      :collapsed="collapsed"
      class="sidebar"
    >
      <div class="brand" :class="{ collapsed }">
        <div class="brand-mark" aria-hidden="true"><Boxes :size="20" /></div>
        <div v-if="!collapsed" class="brand-copy">
          <strong>FORGEDECK</strong>
          <span>DEVOPS CONTROL</span>
        </div>
      </div>
      <NMenu
        :collapsed="collapsed"
        :collapsed-width="74"
        :collapsed-icon-size="20"
        :value="activeKey"
        :options="menuOptions"
        class="nav-menu"
        @update:value="selectMenu"
      />
      <div class="sider-footer">
        <NTooltip placement="right" :disabled="!collapsed">
          <template #trigger>
            <button class="runner-state" type="button">
              <span class="runner-pulse" />
              <span v-if="!collapsed"><b>RUNNER</b><small>等待任务</small></span>
            </button>
          </template>
          Runner 等待任务
        </NTooltip>
      </div>
    </NLayoutSider>

    <NDrawer v-model:show="mobileOpen" placement="left" :width="280">
      <NDrawerContent body-content-style="padding: 0" closable>
        <div class="brand drawer-brand">
          <div class="brand-mark"><Boxes :size="20" /></div>
          <div class="brand-copy"><strong>FORGEDECK</strong><span>DEVOPS CONTROL</span></div>
        </div>
        <NMenu :value="activeKey" :options="menuOptions" @update:value="selectMenu" />
      </NDrawerContent>
    </NDrawer>

    <NLayout>
      <header class="topbar">
        <div class="topbar-left">
          <NButton
            v-if="compact"
            quaternary
            circle
            aria-label="打开导航"
            @click="mobileOpen = true"
          >
            <template #icon><NIcon :component="Menu" /></template>
          </NButton>
          <NButton
            v-else
            quaternary
            circle
            :aria-label="collapsed ? '展开导航' : '收起导航'"
            @click="collapsed = !collapsed"
          >
            <template #icon><NIcon :component="Menu" /></template>
          </NButton>
          <div class="route-crumb">
            <GitBranch :size="14" aria-hidden="true" />
            <span>main</span>
            <i>/</i>
            <strong>{{ route.meta.title }}</strong>
          </div>
        </div>
        <div class="topbar-right">
          <div class="api-state desktop-only"><Activity :size="13" /><span>API ONLINE</span></div>
          <NButton
            quaternary
            circle
            aria-label="通知"
            @click="router.push({ name: 'integrations' })"
          >
            <template #icon><NIcon :component="BellRing" /></template>
          </NButton>
          <NDropdown :options="userOptions" trigger="click" @select="selectUser">
            <button class="user-trigger" type="button">
              <NAvatar round :size="30">{{
                session.user?.username?.slice(0, 1).toUpperCase() || 'A'
              }}</NAvatar>
              <span class="desktop-only">{{ session.user?.username || '管理员' }}</span>
              <ChevronDown class="desktop-only" :size="13" />
            </button>
          </NDropdown>
        </div>
      </header>
      <NLayoutContent class="content" content-style="min-height: calc(100vh - 58px)">
        <RouterView />
      </NLayoutContent>
    </NLayout>
  </NLayout>
</template>

<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { MenuOption } from 'naive-ui'
import {
  NAvatar,
  NButton,
  NDrawer,
  NDrawerContent,
  NDropdown,
  NIcon,
  NLayout,
  NLayoutContent,
  NLayoutSider,
  NMenu,
  NTooltip,
} from 'naive-ui'
import {
  Activity,
  BellRing,
  Boxes,
  Braces,
  ChevronDown,
  CircleGauge,
  Container,
  FileKey2,
  GitBranch,
  History,
  Menu,
  Network,
  PlaySquare,
  ScrollText,
  Server,
  ShieldCheck,
  TerminalSquare,
  Workflow,
  LogOut,
} from 'lucide-vue-next'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const collapsed = ref(false)
const mobileOpen = ref(false)
const compact = ref(false)

function icon(component: Component) {
  /** 为菜单图标统一提供渲染组件引用。 */
  return () => h(NIcon, null, { default: () => h(component) })
}

const menuOptions: MenuOption[] = [
  { label: '运行总览', key: 'dashboard', icon: icon(CircleGauge) },
  { label: '项目', key: 'projects', icon: icon(Braces) },
  { label: '流水线', key: 'pipelines', icon: icon(Workflow) },
  { label: '部署', key: 'deployments', icon: icon(PlaySquare) },
  { type: 'divider', key: 'infra-divider' },
  { label: '服务器', key: 'servers', icon: icon(Server) },
  { label: 'Docker', key: 'docker', icon: icon(Container) },
  { label: 'SSH 终端', key: 'ssh', icon: icon(TerminalSquare) },
  { label: '脚本库', key: 'scripts', icon: icon(ScrollText) },
  { type: 'divider', key: 'security-divider' },
  { label: '凭据', key: 'credentials', icon: icon(FileKey2) },
  { label: '审批中心', key: 'approvals', icon: icon(ShieldCheck) },
  { label: '通知与 MCP', key: 'integrations', icon: icon(Network) },
  { label: '审计日志', key: 'audit', icon: icon(History) },
]

const activeKey = computed(() => {
  if (route.name === 'run-detail') return 'pipelines'
  return String(route.name || 'dashboard')
})

const userOptions = [{ label: '退出登录', key: 'logout', icon: icon(LogOut) }]

function selectMenu(key: string): void {
  /** 将菜单键映射到路由并关闭移动端抽屉。 */
  mobileOpen.value = false
  void router.push({ name: key })
}

function selectUser(key: string): void {
  /** 处理用户菜单动作，目前仅支持退出登录。 */
  if (key === 'logout') {
    session.logout()
    void router.replace({ name: 'login' })
  }
}

function updateViewport(): void {
  /** 根据窗口宽度切换紧凑布局，保持导航可用。 */
  compact.value = window.innerWidth < 900
  if (window.innerWidth < 1180 && window.innerWidth >= 900) collapsed.value = true
}

onMounted(() => {
  updateViewport()
  window.addEventListener('resize', updateViewport)
})
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))
</script>

<style scoped>
.shell {
  min-height: 100vh;
  background: transparent;
}
.sidebar {
  position: sticky;
  top: 0;
  height: 100vh;
  background: rgba(9, 13, 19, 0.97);
}
.brand {
  display: flex;
  align-items: center;
  gap: 11px;
  height: 74px;
  padding: 0 18px;
}
.brand.collapsed {
  justify-content: center;
  padding: 0;
}
.brand-mark {
  display: grid;
  flex: 0 0 auto;
  width: 38px;
  height: 38px;
  border: 1px solid rgba(199, 255, 74, 0.24);
  border-radius: 10px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.08);
  place-items: center;
  transform: rotate(-3deg);
}
.brand-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.brand-copy strong {
  font-size: 14px;
  letter-spacing: 0.08em;
}
.brand-copy span {
  margin-top: 2px;
  color: #5f6c7b;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
  letter-spacing: 0.18em;
}
.nav-menu {
  height: calc(100vh - 142px);
  padding: 6px 10px;
}
.sider-footer {
  position: absolute;
  right: 12px;
  bottom: 13px;
  left: 12px;
}
.runner-state {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid #222d38;
  border-radius: 10px;
  color: #8f9bab;
  background: #0c1219;
  cursor: default;
  text-align: left;
}
.runner-pulse {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: #50d890;
  box-shadow: 0 0 9px rgba(80, 216, 144, 0.8);
}
.runner-state span:not(.runner-pulse) {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.runner-state b {
  color: #b7c2ce;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.12em;
}
.runner-state small {
  margin-top: 2px;
  color: #5e6b78;
  font-size: 10px;
}
.drawer-brand {
  border-bottom: 1px solid #222b36;
}
.topbar {
  position: sticky;
  z-index: 20;
  top: 0;
  display: flex;
  height: 58px;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  border-bottom: 1px solid rgba(35, 44, 56, 0.88);
  background: rgba(8, 11, 16, 0.82);
  backdrop-filter: blur(18px);
}
.topbar-left,
.topbar-right,
.route-crumb,
.api-state,
.user-trigger {
  display: flex;
  align-items: center;
}
.topbar-left,
.topbar-right {
  gap: 10px;
}
.route-crumb {
  gap: 8px;
  margin-left: 4px;
  color: #647181;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}
.route-crumb i {
  color: #333e4a;
  font-style: normal;
}
.route-crumb strong {
  color: #aeb9c6;
  font-weight: 500;
}
.api-state {
  gap: 6px;
  margin-right: 8px;
  color: #50d890;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.1em;
}
.user-trigger {
  gap: 8px;
  padding: 3px 5px 3px 3px;
  border: 0;
  border-radius: 999px;
  color: #b8c3cf;
  background: transparent;
  cursor: pointer;
  font-size: 12px;
}
.user-trigger:hover {
  background: #151c25;
}
.content {
  background: transparent;
}
@media (max-width: 900px) {
  .topbar {
    padding: 0 12px;
  }
  .route-crumb span,
  .route-crumb i {
    display: none;
  }
}
</style>
