<!-- Docker 管理页：查看容器资源并对危险操作执行影响预览和确认。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="CONTAINER RUNTIME"
      title="Docker"
      description="统一查看容器、镜像、卷与网络。危险删除先核对可见依赖，再由 Runner 做最终阻断。"
    >
      <template #actions>
        <NSelect
          v-model:value="serverId"
          class="server-select"
          placeholder="选择服务器"
          :options="servers.map((server) => ({ label: server.name, value: server.id }))"
          @update:value="changeServer"
        />
        <NButton secondary :loading="loading" @click="loadDocker"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        >
      </template>
    </PageHeader>

    <div v-if="loading && !overview" class="loading-block"><NSpin size="large" /></div>
    <template v-else-if="overview">
      <section class="metric-grid">
        <MetricCard
          label="运行中容器"
          :value="runningCount"
          :caption="`${stoppedCount} 个已停止`"
          :icon="Container"
        />
        <MetricCard
          label="本地镜像"
          :value="images.length"
          :caption="`Engine ${version}`"
          :icon="ImageIcon"
          tone="blue"
        />
        <MetricCard
          label="数据卷"
          :value="volumes.length"
          caption="持久化存储"
          :icon="HardDrive"
          tone="amber"
        />
        <MetricCard
          label="Docker 网络"
          :value="networks.length"
          caption="含内置网络"
          :icon="Network"
          tone="blue"
        />
      </section>

      <section class="compose-panel panel">
        <div class="compose-heading">
          <span class="compose-icon"><Layers3 :size="19" /></span>
          <div>
            <p class="eyebrow">COMPOSE CONTROL</p>
            <h2>环境级操作</h2>
            <small>只允许操作绑定到 {{ selectedServer?.name || '当前服务器' }} 的环境</small>
          </div>
        </div>
        <div class="compose-controls">
          <NSelect
            v-model:value="environmentId"
            class="environment-select"
            :loading="environmentsLoading"
            :disabled="composeLoading"
            placeholder="选择部署环境"
            :options="
              environments.map((environment) => ({
                label: `${environment.project_name} / ${environment.name}`,
                value: environment.id,
              }))
            "
          />
          <NButton
            type="primary"
            secondary
            :disabled="!environmentId"
            :loading="composeLoading"
            @click="requestCompose('up')"
            ><template #icon><NIcon :component="Play" /></template>Compose Up</NButton
          >
          <NButton
            secondary
            :disabled="!environmentId"
            :loading="composeLoading"
            @click="requestCompose('restart')"
            ><template #icon><NIcon :component="RotateCw" /></template>Restart</NButton
          >
          <NButton
            type="error"
            secondary
            :disabled="!environmentId"
            :loading="composeLoading"
            @click="requestCompose('down')"
            ><template #icon><NIcon :component="Square" /></template>Compose Down</NButton
          >
        </div>
        <div v-if="selectedEnvironment" class="environment-meta mono">
          <span><ServerCog :size="12" />{{ selectedEnvironment.deploy_path }}</span>
          <span
            ><Link2 :size="12" />{{ selectedEnvironment.compose_source }} /
            {{ selectedEnvironment.compose_path }}</span
          >
        </div>
      </section>

      <section class="panel docker-table">
        <NTabs type="line" animated pane-style="padding-top: 12px">
          <NTabPane name="containers" :tab="`容器 ${containers.length}`"
            ><NDataTable
              :data="containers"
              :columns="containerColumns"
              :bordered="false"
              :single-line="false"
              :scroll-x="1180"
          /></NTabPane>
          <NTabPane name="images" :tab="`镜像 ${images.length}`"
            ><NDataTable
              :data="images"
              :columns="imageColumns"
              :bordered="false"
              :single-line="false"
              :scroll-x="920"
          /></NTabPane>
          <NTabPane name="volumes" :tab="`数据卷 ${volumes.length}`"
            ><NDataTable
              :data="volumes"
              :columns="volumeColumns"
              :bordered="false"
              :single-line="false"
              :scroll-x="850"
          /></NTabPane>
          <NTabPane name="networks" :tab="`网络 ${networks.length}`"
            ><NDataTable
              :data="networks"
              :columns="networkColumns"
              :bordered="false"
              :single-line="false"
              :scroll-x="820"
          /></NTabPane>
          <NTabPane name="disk" :tab="`空间 ${overview.disk_usage.length}`"
            ><NDataTable
              :data="overview.disk_usage"
              :columns="diskColumns"
              :bordered="false"
              :single-line="false"
              :scroll-x="680"
          /></NTabPane>
        </NTabs>
      </section>
    </template>
    <section v-else class="panel">
      <EmptyState
        :icon="Box"
        title="没有 Docker 数据"
        description="请选择在线服务器，并确认 Runner 可以通过 SSH 访问 Docker Engine。"
      />
    </section>

    <NModal
      :show="Boolean(deleteIntent)"
      preset="card"
      class="delete-modal"
      :bordered="false"
      :mask-closable="false"
      @update:show="
        (value) => {
          if (!value) deleteIntent = null
        }
      "
    >
      <template #header>
        <div class="delete-title">
          <span><AlertTriangle :size="20" /></span>
          <div>
            <p class="eyebrow">DESTRUCTIVE ACTION</p>
            <h2>删除{{ deleteIntent?.kindLabel }}</h2>
          </div>
        </div>
      </template>
      <NAlert v-if="deleteIntent?.blocked" type="error" :bordered="false">{{
        deleteIntent.blockReason
      }}</NAlert>
      <div class="impact-block">
        <h3>依赖影响预览</h3>
        <p>以下信息来自当前 Docker overview；Runner 会在执行时再次检查真实依赖。</p>
        <ul>
          <li v-for="item in deleteIntent?.impact" :key="item">{{ item }}</li>
        </ul>
      </div>
      <div class="confirm-block">
        <label
          >输入目标名称 <code>{{ deleteIntent?.target }}</code> 完全确认</label
        >
        <NInput
          v-model:value="deleteConfirmation"
          :disabled="deleteIntent?.blocked"
          :placeholder="deleteIntent?.target"
          class="mono"
        />
      </div>
      <template #footer>
        <div class="modal-footer">
          <NButton @click="deleteIntent = null">取消</NButton
          ><NButton
            type="error"
            :loading="deleting"
            :disabled="deleteIntent?.blocked || deleteConfirmation !== deleteIntent?.target"
            @click="confirmDelete"
            ><template #icon><NIcon :component="Trash2" /></template>永久删除</NButton
          >
        </div>
      </template>
    </NModal>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NDataTable,
  NIcon,
  NInput,
  NModal,
  NSelect,
  NSpin,
  NTabPane,
  NTabs,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import {
  AlertTriangle,
  Box,
  Container,
  HardDrive,
  Image as ImageIcon,
  Layers3,
  Link2,
  Network,
  Play,
  RefreshCw,
  RotateCw,
  ServerCog,
  Square,
  Trash2,
} from 'lucide-vue-next'
import { api } from '@/api/client'
import type {
  DockerContainer,
  DockerImage,
  DockerNetwork,
  DockerOverview,
  DockerVolume,
  Environment,
  Project,
  Server,
} from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import MetricCard from '@/components/MetricCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatDate } from '@/utils/format'

type DeleteKind = 'container' | 'image' | 'volume' | 'network'
type ManagedEnvironment = Environment & { project_name: string }

interface DeleteIntent {
  kind: DeleteKind
  kindLabel: string
  target: string
  impact: string[]
  blocked: boolean
  blockReason?: string
}

const message = useMessage()
const dialog = useDialog()
const servers = ref<Server[]>([])
const serverId = ref<string | null>(null)
const overview = ref<DockerOverview | null>(null)
const loading = ref(false)
const composeLoading = ref(false)
const environmentsLoading = ref(false)
const environments = ref<ManagedEnvironment[]>([])
const environmentId = ref<string | null>(null)
const deleteIntent = ref<DeleteIntent | null>(null)
const deleteConfirmation = ref('')
const deleting = ref(false)

const containers = computed(() => overview.value?.containers || [])
const images = computed(() => overview.value?.images || [])
const volumes = computed(() => overview.value?.volumes || [])
const networks = computed(() => overview.value?.networks || [])
const runningCount = computed(
  () => containers.value.filter((item) => containerState(item) === 'running').length,
)
const stoppedCount = computed(() => containers.value.length - runningCount.value)
const selectedEnvironment = computed(
  () => environments.value.find((item) => item.id === environmentId.value) || null,
)
const selectedServer = computed(
  () => servers.value.find((item) => item.id === serverId.value) || null,
)
const version = computed(() => {
  const value = overview.value?.version
  if (!value) return '—'
  return stringValue(value, 'Version', 'ServerVersion', 'version') || '已连接'
})

function stringValue(row: Record<string, unknown>, ...keys: string[]): string {
  /** 从不同 Docker CLI/SDK 字段名中读取首个可展示字符串。 */
  for (const key of keys) {
    const value = row[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (typeof value === 'number') return String(value)
  }
  return ''
}

function splitResources(value: string): string[] {
  /** 将 Docker 表格中的逗号或换行资源列表拆成展示数组。 */
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function containerName(row: DockerContainer): string {
  /** 提取容器名称并去除 CLI 可能返回的前导斜杠。 */
  return stringValue(row, 'Names', 'Name', 'name', 'ID', 'id')
}

function containerId(row: DockerContainer): string {
  /** 提取容器短 ID。 */
  return stringValue(row, 'ID', 'id')
}

function containerImage(row: DockerContainer): string {
  /** 提取容器使用的镜像引用。 */
  return stringValue(row, 'Image', 'image')
}

function containerState(row: DockerContainer): string {
  /** 提取容器运行状态字段。 */
  return stringValue(row, 'State', 'state').toLowerCase()
}

function containerStatus(row: DockerContainer): string {
  /** 提取 Docker 生成的状态描述。 */
  return stringValue(row, 'Status', 'status') || containerState(row)
}

function containerMounts(row: DockerContainer): string[] {
  /** 归一化容器挂载点，供依赖影响预览使用。 */
  return splitResources(stringValue(row, 'Mounts', 'mounts'))
}

function containerNetworks(row: DockerContainer): string[] {
  /** 归一化容器网络列表。 */
  return splitResources(stringValue(row, 'Networks', 'networks'))
}

function imageTarget(row: DockerImage): string {
  /** 选择删除镜像时使用的稳定目标引用。 */
  const repository = stringValue(row, 'Repository', 'repository')
  const tag = stringValue(row, 'Tag', 'tag')
  if (repository && repository !== '<none>')
    return tag && tag !== '<none>' ? `${repository}:${tag}` : repository
  return stringValue(row, 'ID', 'id', 'Name', 'name')
}

function imageLabel(row: DockerImage): string {
  /** 将镜像标签集合合并为单行展示文本。 */
  return imageTarget(row) || '<none>'
}

function volumeName(row: DockerVolume): string {
  /** 提取卷名称。 */
  return stringValue(row, 'Name', 'name')
}

function networkName(row: DockerNetwork): string {
  /** 提取网络名称。 */
  return stringValue(row, 'Name', 'name')
}

function containersUsingImage(row: DockerImage): DockerContainer[] {
  /** 查找引用指定镜像的容器，删除前展示依赖关系。 */
  const target = imageTarget(row)
  const repository = stringValue(row, 'Repository', 'repository')
  const id = stringValue(row, 'ID', 'id').replace(/^sha256:/, '')
  return containers.value.filter((container) => {
    const image = containerImage(container)
    return (
      image === target ||
      image === repository ||
      (id.length >= 12 && image.includes(id.slice(0, 12)))
    )
  })
}

function containersUsingVolume(row: DockerVolume): DockerContainer[] {
  /** 查找挂载指定卷的容器。 */
  const name = volumeName(row)
  return containers.value.filter((container) => containerMounts(container).includes(name))
}

function containersUsingNetwork(row: DockerNetwork): DockerContainer[] {
  /** 查找连接指定网络的容器。 */
  const name = networkName(row)
  return containers.value.filter((container) => containerNetworks(container).includes(name))
}

async function loadServers(): Promise<void> {
  /** 加载服务器并选择 Docker 管理目标。 */
  try {
    servers.value = await api.servers.list()
    serverId.value ||= servers.value[0]?.id || null
    if (serverId.value) await Promise.all([loadDocker(), loadEnvironments()])
  } catch (error) {
    message.error(error instanceof Error ? error.message : '服务器加载失败')
  }
}

async function loadDocker(): Promise<void> {
  /** 读取当前服务器 Docker 资源和空间概览。 */
  if (!serverId.value) return
  loading.value = true
  try {
    overview.value = await api.docker.overview(serverId.value)
  } catch (error) {
    overview.value = null
    message.error(error instanceof Error ? error.message : 'Docker 状态加载失败')
  } finally {
    loading.value = false
  }
}

async function loadEnvironments(): Promise<void> {
  /** 加载服务器绑定环境，为 Compose 操作提供项目目录。 */
  if (!serverId.value) return
  environmentsLoading.value = true
  environmentId.value = null
  try {
    const projects = await api.projects.list()
    const results = await Promise.allSettled(
      projects.map(async (project: Project) => ({
        project,
        environments: await api.projects.environments(project.id),
      })),
    )
    const groups = results.flatMap((result) =>
      result.status === 'fulfilled' ? [result.value] : [],
    )
    const failedCount = results.length - groups.length
    if (failedCount)
      message.warning(`${failedCount} 个项目的 Compose 环境加载失败，当前列表可能不完整`)
    environments.value = groups.flatMap(({ project, environments: projectEnvironments }) =>
      projectEnvironments
        .filter((environment) => environment.server_id === serverId.value)
        .map((environment) => ({ ...environment, project_name: project.name })),
    )
    environmentId.value = environments.value[0]?.id || null
  } catch (error) {
    environments.value = []
    message.warning(error instanceof Error ? error.message : 'Compose 环境加载失败')
  } finally {
    environmentsLoading.value = false
  }
}

async function changeServer(): Promise<void> {
  /** 切换服务器后清空旧资源并重新加载概览。 */
  overview.value = null
  await Promise.all([loadDocker(), loadEnvironments()])
}

async function containerAction(
  container: DockerContainer,
  action: 'start' | 'stop' | 'restart',
): Promise<void> {
  /** 执行受白名单限制的容器启停操作。 */
  if (!serverId.value) return
  try {
    await api.docker.containerAction(serverId.value, containerName(container), action)
    message.success(`容器 ${containerName(container)} 已执行 ${action}`)
    await loadDocker()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '容器操作失败')
  }
}

function openDelete(
  kind: DeleteKind,
  row: DockerContainer | DockerImage | DockerVolume | DockerNetwork,
): void {
  /** 打开危险删除确认，并展示受影响的容器依赖。 */
  let intent: DeleteIntent
  if (kind === 'container') {
    const container = row as DockerContainer
    const running = containerState(container) === 'running'
    const mounts = containerMounts(container)
    const attachedNetworks = containerNetworks(container)
    intent = {
      kind,
      kindLabel: '容器',
      target: containerName(container),
      impact: [
        `镜像：${containerImage(container) || '未知'}`,
        `挂载：${mounts.length ? mounts.join('、') : '无可见挂载'}`,
        `网络：${attachedNetworks.length ? attachedNetworks.join('、') : '无可见网络'}`,
        `端口：${stringValue(container, 'Ports', 'ports') || '未暴露'}`,
      ],
      blocked: running,
      blockReason: running ? '该容器仍在运行，必须先停止后才能删除。' : undefined,
    }
  } else if (kind === 'image') {
    const image = row as DockerImage
    const users = containersUsingImage(image)
    intent = {
      kind,
      kindLabel: '镜像',
      target: imageTarget(image),
      impact: [
        `镜像 ID：${stringValue(image, 'ID', 'id') || '未知'}`,
        `摘要：${stringValue(image, 'Digest', 'digest') || '未提供'}`,
        `可见关联容器：${users.length ? users.map(containerName).join('、') : '未发现'}`,
      ],
      blocked: users.length > 0,
      blockReason: users.length ? '该镜像仍被容器引用，请先删除关联容器。' : undefined,
    }
  } else if (kind === 'volume') {
    const volume = row as DockerVolume
    const users = containersUsingVolume(volume)
    intent = {
      kind,
      kindLabel: '数据卷',
      target: volumeName(volume),
      impact: [
        `挂载点：${stringValue(volume, 'Mountpoint', 'mountpoint') || '未提供'}`,
        `驱动：${stringValue(volume, 'Driver', 'driver') || '未知'}`,
        `可见关联容器：${users.length ? users.map(containerName).join('、') : '未发现'}`,
      ],
      blocked: users.length > 0,
      blockReason: users.length ? '该卷仍挂载到容器，删除会被 Runner 拒绝。' : undefined,
    }
  } else {
    const network = row as DockerNetwork
    const users = containersUsingNetwork(network)
    const name = networkName(network)
    const builtin = ['bridge', 'host', 'none'].includes(name)
    intent = {
      kind,
      kindLabel: '网络',
      target: name,
      impact: [
        `驱动：${stringValue(network, 'Driver', 'driver') || '未知'}`,
        `范围：${stringValue(network, 'Scope', 'scope') || '未知'}`,
        `可见关联容器：${users.length ? users.map(containerName).join('、') : '未发现'}`,
      ],
      blocked: builtin || users.length > 0,
      blockReason: builtin
        ? 'Docker 内置网络不可删除。'
        : users.length
          ? '该网络仍连接容器，删除会被 Runner 拒绝。'
          : undefined,
    }
  }
  deleteConfirmation.value = ''
  deleteIntent.value = intent
}

async function confirmDelete(): Promise<void> {
  /** 提交精确名称确认后的 Docker 删除请求。 */
  if (!serverId.value || !deleteIntent.value || deleteIntent.value.blocked) return
  if (deleteConfirmation.value !== deleteIntent.value.target) return
  deleting.value = true
  try {
    await api.docker.remove(
      serverId.value,
      deleteIntent.value.kind,
      deleteIntent.value.target,
      deleteConfirmation.value,
    )
    message.success(`${deleteIntent.value.kindLabel} ${deleteIntent.value.target} 已删除`)
    deleteIntent.value = null
    await loadDocker()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '删除失败')
  } finally {
    deleting.value = false
  }
}

async function executeCompose(action: 'up' | 'down' | 'restart'): Promise<void> {
  /** 对选中环境执行 Compose 生命周期操作并刷新状态。 */
  if (!serverId.value || !environmentId.value) {
    message.warning('请选择绑定到当前服务器的部署环境')
    return
  }
  composeLoading.value = true
  try {
    await api.docker.composeAction(serverId.value, action, environmentId.value)
    const labels = { up: '启动', down: '停止', restart: '重启' }
    message.success(`Compose ${labels[action]}完成`)
    await loadDocker()
  } catch (error) {
    message.error(error instanceof Error ? error.message : 'Compose 操作失败')
  } finally {
    composeLoading.value = false
  }
}

function requestCompose(action: 'up' | 'down' | 'restart'): void {
  /** 打开 Compose 操作确认对话框，避免误操作生产环境。 */
  if (!selectedEnvironment.value) {
    message.warning('请选择部署环境')
    return
  }
  if (action === 'down') {
    dialog.warning({
      title: '停止 Compose 项目',
      content: `将停止“${selectedEnvironment.value.project_name} / ${selectedEnvironment.value.name}”的全部服务。数据卷不会自动删除。`,
      positiveText: '确认停止',
      negativeText: '取消',
      onPositiveClick: () => executeCompose('down'),
    })
    return
  }
  void executeCompose(action)
}

const containerColumns: DataTableColumns<DockerContainer> = [
  {
    title: '容器',
    key: 'name',
    minWidth: 170,
    render: (row) =>
      h('div', { class: 'docker-name' }, [
        h('strong', containerName(row)),
        h('small', { class: 'mono' }, containerId(row).slice(0, 12) || '—'),
      ]),
  },
  {
    title: '镜像',
    key: 'image',
    minWidth: 240,
    ellipsis: { tooltip: true },
    render: (row) => h('span', { class: 'mono image-ref' }, containerImage(row) || '—'),
  },
  {
    title: '状态',
    key: 'state',
    width: 170,
    render: (row) =>
      h('div', { class: 'state-cell' }, [
        h(StatusBadge, {
          status: containerState(row) === 'running' ? 'online' : containerState(row),
        }),
        h('small', containerStatus(row)),
      ]),
  },
  {
    title: '端口',
    key: 'ports',
    minWidth: 170,
    render: (row) => h('span', { class: 'mono muted' }, stringValue(row, 'Ports', 'ports') || '—'),
  },
  {
    title: '挂载 / 网络',
    key: 'resources',
    minWidth: 220,
    render: (row) =>
      h('div', { class: 'resource-stack' }, [
        h('span', [h(HardDrive, { size: 11 }), containerMounts(row).join(', ') || '无挂载']),
        h('span', [h(Network, { size: 11 }), containerNetworks(row).join(', ') || '无网络']),
      ]),
  },
  {
    title: '',
    key: 'actions',
    width: 160,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'docker-actions' }, [
        containerState(row) === 'running'
          ? h(
              NButton,
              {
                quaternary: true,
                circle: true,
                title: '停止',
                'aria-label': `停止容器 ${containerName(row)}`,
                onClick: () => containerAction(row, 'stop'),
              },
              { icon: () => h(NIcon, { component: Square }) },
            )
          : h(
              NButton,
              {
                quaternary: true,
                circle: true,
                title: '启动',
                'aria-label': `启动容器 ${containerName(row)}`,
                onClick: () => containerAction(row, 'start'),
              },
              { icon: () => h(NIcon, { component: Play }) },
            ),
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            title: '重启',
            'aria-label': `重启容器 ${containerName(row)}`,
            onClick: () => containerAction(row, 'restart'),
          },
          { icon: () => h(NIcon, { component: RotateCw }) },
        ),
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            type: 'error',
            title: '删除',
            'aria-label': `删除容器 ${containerName(row)}`,
            onClick: () => openDelete('container', row),
          },
          { icon: () => h(NIcon, { component: Trash2 }) },
        ),
      ]),
  },
]

const imageColumns: DataTableColumns<DockerImage> = [
  {
    title: '镜像',
    key: 'image',
    minWidth: 270,
    render: (row) =>
      h('div', { class: 'docker-name' }, [
        h('strong', imageLabel(row)),
        h('small', { class: 'mono' }, stringValue(row, 'Digest', 'digest') || '无 digest'),
      ]),
  },
  {
    title: '镜像 ID',
    key: 'id',
    minWidth: 170,
    render: (row) =>
      h(
        'span',
        { class: 'mono muted' },
        stringValue(row, 'ID', 'id')
          .replace(/^sha256:/, '')
          .slice(0, 16) || '—',
      ),
  },
  {
    title: '大小',
    key: 'size',
    width: 110,
    render: (row) => stringValue(row, 'Size', 'size') || '—',
  },
  {
    title: '创建时间',
    key: 'created',
    width: 170,
    render: (row) => formatDate(stringValue(row, 'CreatedAt', 'created_at')),
  },
  {
    title: '依赖容器',
    key: 'dependencies',
    width: 110,
    render: (row) => containersUsingImage(row).length,
  },
  {
    title: '',
    key: 'actions',
    width: 60,
    render: (row) =>
      h(
        NButton,
        {
          quaternary: true,
          circle: true,
          type: 'error',
          'aria-label': `删除镜像 ${imageLabel(row)}`,
          onClick: () => openDelete('image', row),
        },
        { icon: () => h(NIcon, { component: Trash2 }) },
      ),
  },
]

const volumeColumns: DataTableColumns<DockerVolume> = [
  {
    title: '数据卷',
    key: 'name',
    minWidth: 240,
    render: (row) =>
      h('div', { class: 'docker-name' }, [
        h('strong', volumeName(row)),
        h(
          'small',
          { class: 'mono' },
          stringValue(row, 'Mountpoint', 'mountpoint') || '未提供挂载点',
        ),
      ]),
  },
  {
    title: '驱动',
    key: 'driver',
    width: 110,
    render: (row) => stringValue(row, 'Driver', 'driver') || '—',
  },
  {
    title: '范围',
    key: 'scope',
    width: 100,
    render: (row) => stringValue(row, 'Scope', 'scope') || 'local',
  },
  {
    title: '大小',
    key: 'size',
    width: 110,
    render: (row) => stringValue(row, 'Size', 'size') || '—',
  },
  {
    title: '依赖容器',
    key: 'dependencies',
    width: 110,
    render: (row) => containersUsingVolume(row).length,
  },
  {
    title: '',
    key: 'actions',
    width: 60,
    render: (row) =>
      h(
        NButton,
        {
          quaternary: true,
          circle: true,
          type: 'error',
          'aria-label': `删除数据卷 ${volumeName(row)}`,
          onClick: () => openDelete('volume', row),
        },
        { icon: () => h(NIcon, { component: Trash2 }) },
      ),
  },
]

const networkColumns: DataTableColumns<DockerNetwork> = [
  {
    title: '网络',
    key: 'name',
    minWidth: 220,
    render: (row) =>
      h('div', { class: 'docker-name' }, [
        h('strong', networkName(row)),
        h('small', { class: 'mono' }, stringValue(row, 'ID', 'id').slice(0, 16) || '—'),
      ]),
  },
  {
    title: '驱动',
    key: 'driver',
    width: 120,
    render: (row) => stringValue(row, 'Driver', 'driver') || '—',
  },
  {
    title: '范围',
    key: 'scope',
    width: 100,
    render: (row) => stringValue(row, 'Scope', 'scope') || '—',
  },
  {
    title: 'IPv6',
    key: 'ipv6',
    width: 90,
    render: (row) => stringValue(row, 'IPv6', 'ipv6') || 'false',
  },
  {
    title: '依赖容器',
    key: 'dependencies',
    width: 110,
    render: (row) => containersUsingNetwork(row).length,
  },
  {
    title: '',
    key: 'actions',
    width: 60,
    render: (row) =>
      h(
        NButton,
        {
          quaternary: true,
          circle: true,
          type: 'error',
          disabled: ['bridge', 'host', 'none'].includes(networkName(row)),
          'aria-label': `删除网络 ${networkName(row)}`,
          onClick: () => openDelete('network', row),
        },
        { icon: () => h(NIcon, { component: Trash2 }) },
      ),
  },
]

const diskColumns: DataTableColumns<Record<string, unknown>> = [
  {
    title: '类型',
    key: 'Type',
    minWidth: 130,
    render: (row) => stringValue(row, 'Type', 'type') || '—',
  },
  {
    title: '总数',
    key: 'TotalCount',
    width: 100,
    render: (row) => stringValue(row, 'TotalCount', 'total_count') || '—',
  },
  {
    title: '活跃',
    key: 'Active',
    width: 100,
    render: (row) => stringValue(row, 'Active', 'active') || '—',
  },
  {
    title: '占用',
    key: 'Size',
    minWidth: 130,
    render: (row) => stringValue(row, 'Size', 'size') || '—',
  },
  {
    title: '可回收',
    key: 'Reclaimable',
    minWidth: 150,
    render: (row) => stringValue(row, 'Reclaimable', 'reclaimable') || '—',
  },
]

onMounted(loadServers)
</script>

<style scoped>
.server-select {
  width: 220px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 13px;
  margin-bottom: 16px;
}
.compose-panel {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 18px;
  margin-bottom: 16px;
  padding: 17px 18px;
}
.compose-heading {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 240px;
}
.compose-icon {
  display: grid;
  width: 42px;
  height: 42px;
  border: 1px solid rgba(199, 255, 74, 0.2);
  border-radius: 11px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.07);
  place-items: center;
}
.compose-heading h2 {
  margin: 0;
  font-size: 15px;
}
.compose-heading small {
  display: block;
  margin-top: 3px;
  color: #647180;
  font-size: 9px;
}
.compose-controls {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}
.environment-select {
  width: min(340px, 100%);
}
.environment-meta {
  display: flex;
  grid-column: 2;
  justify-content: flex-end;
  gap: 18px;
  color: #596674;
  font-size: 8px;
}
.environment-meta span {
  display: flex;
  align-items: center;
  gap: 5px;
}
.docker-table {
  padding: 8px 16px 16px;
}
:deep(.docker-name) {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
:deep(.docker-name strong) {
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
}
:deep(.docker-name small) {
  overflow: hidden;
  color: #61707f;
  font-size: 8px;
  text-overflow: ellipsis;
}
:deep(.image-ref) {
  color: #8e9baa;
  font-size: 9px;
}
:deep(.state-cell) {
  display: flex;
  align-items: center;
  gap: 7px;
}
:deep(.state-cell small) {
  overflow: hidden;
  color: #687584;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.resource-stack) {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}
:deep(.resource-stack span) {
  display: flex;
  align-items: center;
  gap: 5px;
  overflow: hidden;
  color: #718090;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.docker-actions) {
  display: flex;
  justify-content: flex-end;
  gap: 3px;
}
.delete-modal {
  width: min(620px, calc(100vw - 30px));
}
.delete-title {
  display: flex;
  align-items: center;
  gap: 12px;
}
.delete-title > span {
  display: grid;
  width: 42px;
  height: 42px;
  border-radius: 11px;
  color: #ff637d;
  background: rgba(255, 99, 125, 0.09);
  place-items: center;
}
.delete-title h2 {
  margin: 0;
  font-size: 20px;
}
.impact-block {
  margin-top: 16px;
  padding: 14px;
  border: 1px solid #27313d;
  border-radius: 10px;
  background: #0a0f15;
}
.impact-block h3 {
  margin: 0;
  font-size: 12px;
}
.impact-block p {
  margin: 5px 0 10px;
  color: #657280;
  font-size: 10px;
  line-height: 1.5;
}
.impact-block ul {
  display: grid;
  gap: 7px;
  margin: 0;
  padding-left: 18px;
  color: #929eab;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
}
.confirm-block {
  margin-top: 16px;
}
.confirm-block label {
  display: block;
  margin-bottom: 7px;
  color: #768392;
  font-size: 10px;
}
.confirm-block code {
  color: #ff637d;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
@media (max-width: 1200px) {
  .metric-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .compose-panel {
    grid-template-columns: 1fr;
  }
  .compose-controls {
    justify-content: flex-start;
    flex-wrap: wrap;
  }
  .environment-meta {
    grid-column: 1;
    justify-content: flex-start;
  }
}
@media (max-width: 700px) {
  .metric-grid {
    grid-template-columns: 1fr;
  }
  .server-select,
  .environment-select {
    width: 100%;
  }
  .compose-controls {
    align-items: stretch;
    flex-direction: column;
  }
  .environment-meta {
    align-items: flex-start;
    flex-direction: column;
    gap: 7px;
  }
}
</style>
