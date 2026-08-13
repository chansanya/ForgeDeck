<!-- 服务器页面：登记主机、确认 SSH 指纹并展示资源指标。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="INFRASTRUCTURE"
      title="服务器"
      description="通过 SSH 采集系统指标并执行受控部署。首次连接必须确认主机指纹，别拿安全当摆设。"
    >
      <template #actions
        ><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ><NButton type="primary" @click="openCreate"
          ><template #icon><NIcon :component="Plus" /></template>登记服务器</NButton
        ></template
      >
    </PageHeader>

    <div v-if="loading && !servers.length" class="loading-block"><NSpin size="large" /></div>
    <div v-else-if="servers.length" class="server-layout">
      <aside class="server-list panel">
        <button
          v-for="server in servers"
          :key="server.id"
          type="button"
          :class="{ active: selectedId === server.id }"
          @click="selectedId = server.id"
        >
          <span class="server-avatar"><ServerIcon :size="17" /></span
          ><span class="server-copy"
            ><b>{{ server.name }}</b
            ><small class="mono"
              >{{ server.username }}@{{ server.host }}:{{ server.port }}</small
            ></span
          ><StatusBadge :status="serverStatus(server)" />
        </button>
      </aside>

      <main v-if="selected" class="server-main">
        <section class="node-head panel">
          <div>
            <span class="node-icon"><ServerIcon :size="23" /></span>
            <div>
              <p class="eyebrow">NODE / {{ selected.id.slice(0, 8).toUpperCase() }}</p>
              <h2>{{ selected.name }}</h2>
              <span class="mono endpoint"
                >{{ selected.username }}@{{ selected.host }}:{{ selected.port }}</span
              >
            </div>
          </div>
          <div class="node-actions">
            <NButton secondary @click="test"
              ><template #icon><NIcon :component="Wifi" /></template>连接检查</NButton
            ><NButton
              type="primary"
              secondary
              @click="router.push({ name: 'ssh', params: { serverId: selected.id } })"
              ><template #icon><NIcon :component="TerminalSquare" /></template>SSH 终端</NButton
            ><NButton quaternary circle aria-label="编辑服务器" @click="openEdit(selected)"
              ><template #icon><NIcon :component="Pencil" /></template></NButton
            ><NButton
              quaternary
              circle
              type="error"
              aria-label="移除服务器"
              @click="remove(selected)"
              ><template #icon><NIcon :component="Trash2" /></template
            ></NButton>
          </div>
        </section>

        <section class="resource-grid">
          <article class="resource panel">
            <div><Cpu :size="17" /><span>CPU</span></div>
            <strong>{{ latest?.cpu_percent?.toFixed(1) || '0.0' }}%</strong
            ><NProgress
              type="line"
              :percentage="latest?.cpu_percent || 0"
              :show-indicator="false"
              color="#c7ff4a"
              rail-color="#202a35"
            /><small>{{ latest?.cpu_cores || '—' }} 核</small>
          </article>
          <article class="resource panel">
            <div><MemoryStick :size="17" /><span>内存</span></div>
            <strong>{{ formatBytes(latest?.memory_used) }}</strong
            ><NProgress
              type="line"
              :percentage="percent(latest?.memory_used, latest?.memory_total)"
              :show-indicator="false"
              color="#5ea1ff"
              rail-color="#202a35"
            /><small>共 {{ formatBytes(latest?.memory_total) }}</small>
          </article>
          <article class="resource panel">
            <div><HardDrive :size="17" /><span>磁盘</span></div>
            <strong>{{ formatBytes(latest?.disk_used) }}</strong
            ><NProgress
              type="line"
              :percentage="percent(latest?.disk_used, latest?.disk_total)"
              :show-indicator="false"
              color="#f5b942"
              rail-color="#202a35"
            /><small>共 {{ formatBytes(latest?.disk_total) }}</small>
          </article>
          <article class="resource panel">
            <div><Network :size="17" /><span>网络累计</span></div>
            <strong>{{
              formatBytes((latest?.network_rx || 0) + (latest?.network_tx || 0))
            }}</strong>
            <div class="net-row">
              <span>RX {{ formatBytes(latest?.network_rx) }}</span
              ><span>TX {{ formatBytes(latest?.network_tx) }}</span>
            </div>
            <small>最近采集 {{ formatDate(selected.last_seen_at) }}</small>
          </article>
        </section>

        <section class="panel metrics-panel">
          <div class="section-heading">
            <div>
              <p class="eyebrow">24H TELEMETRY</p>
              <h2>资源趋势</h2>
            </div>
            <NSpin v-if="metricsLoading" size="small" />
          </div>
          <ChartPanel :option="metricsOption" :height="300" />
        </section>
        <section class="panel fingerprint">
          <Activity :size="17" />
          <div>
            <b>SSH 主机指纹</b><code>{{ selected.host_key || '尚未确认' }}</code>
          </div>
          <span>{{ selected.enabled ? 'ENABLED' : 'DISABLED' }}</span>
        </section>
      </main>
    </div>
    <section v-else class="panel">
      <EmptyState
        :icon="ServerIcon"
        title="尚未登记服务器"
        description="添加受管 Linux 主机后，才能采集指标、管理 Docker 和执行部署。"
        action-label="登记服务器"
        @action="openCreate"
      />
    </section>

    <NModal
      v-model:show="showEditor"
      preset="card"
      :title="editingId ? '编辑服务器' : '登记服务器'"
      class="server-modal"
      :bordered="false"
      @after-leave="resetServerForm"
    >
      <NForm ref="formRef" :model="form" :rules="rules" label-placement="top">
        <NFormItem label="节点名称" path="name"
          ><NInput v-model:value="form.name" placeholder="prod-cn-01"
        /></NFormItem>
        <div class="form-grid">
          <NFormItem label="主机 / IP" path="host"
            ><NInput v-model:value="form.host" placeholder="10.0.0.12" /></NFormItem
          ><NFormItem label="端口" path="port"
            ><NInputNumber v-model:value="form.port" :min="1" :max="65535"
          /></NFormItem>
        </div>
        <div class="form-grid">
          <NFormItem label="SSH 用户" path="username"
            ><NInput v-model:value="form.username" placeholder="deployer" /></NFormItem
          ><NFormItem label="SSH 凭据" path="ssh_credential_id"
            ><NSelect
              v-model:value="form.ssh_credential_id"
              placeholder="选择已加密凭据"
              :options="credentials.map((item) => ({ label: item.name, value: item.id }))"
          /></NFormItem>
        </div>
        <section class="host-key-panel">
          <div class="host-key-heading">
            <div>
              <span><Fingerprint :size="17" /></span>
              <div>
                <b>SSH 主机密钥</b><small>扫描只读取服务器公开密钥，不会使用 SSH 登录凭据。</small>
              </div>
            </div>
            <NButton
              secondary
              :loading="scanningHostKey"
              :disabled="!form.host"
              @click="scanHostKey"
              ><template #icon><NIcon :component="ScanLine" /></template
              >{{ editingId && form.host_key ? '重新扫描' : '扫描指纹' }}</NButton
            >
          </div>
          <NAlert
            v-if="!scannedHostKey && editingId && originalHostKey && !endpointChanged"
            type="success"
            :bordered="false"
          >
            当前已固定主机密钥：<code class="current-host-key">{{ originalHostKey }}</code
            >。可重新扫描以完成密钥轮换。
          </NAlert>
          <NAlert
            v-else-if="!scannedHostKey"
            :type="form.enabled ? 'warning' : 'info'"
            :bordered="false"
          >
            {{
              endpointChanged
                ? '主机或端口已变化，旧指纹已失效；重新启用前必须扫描并核对新指纹。'
                : form.enabled
                  ? '启用节点前必须扫描，并通过可信渠道核对指纹。'
                  : '停用节点可以暂不固定指纹，启用前仍必须完成确认。'
            }}
          </NAlert>
          <div v-else class="host-key-result">
            <dl>
              <div>
                <dt>算法</dt>
                <dd>
                  <code>{{ scannedHostKey.algorithm }}</code>
                </dd>
              </div>
              <div>
                <dt>SHA-256 指纹</dt>
                <dd>
                  <code>{{ scannedHostKey.fingerprint }}</code>
                </dd>
              </div>
              <div class="public-key">
                <dt>OpenSSH 公钥</dt>
                <dd>
                  <code>{{ scannedHostKey.public_key }}</code>
                </dd>
              </div>
            </dl>
            <NCheckbox v-model:checked="hostKeyConfirmed">
              我已通过服务器控制台或可信渠道核对以上指纹
            </NCheckbox>
          </div>
        </section>
        <div class="switch-row">
          <div>
            <strong>启用服务器</strong
            ><span>启用后 Runner 才会采集指标并允许部署任务命中该节点。</span>
          </div>
          <NSwitch v-model:value="form.enabled" />
        </div>
      </NForm>
      <template #footer
        ><div class="modal-footer">
          <NButton @click="showEditor = false">取消</NButton
          ><NButton type="primary" :loading="saving" @click="save">保存服务器</NButton>
        </div></template
      >
    </NModal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  NAlert,
  NButton,
  NCheckbox,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
  NModal,
  NProgress,
  NSelect,
  NSpin,
  NSwitch,
  useDialog,
  useMessage,
  type FormInst,
  type FormRules,
} from 'naive-ui'
import {
  Activity,
  Cpu,
  Fingerprint,
  HardDrive,
  MemoryStick,
  Network,
  Pencil,
  Plus,
  RefreshCw,
  ScanLine,
  Server as ServerIcon,
  TerminalSquare,
  Trash2,
  Wifi,
} from 'lucide-vue-next'
import type { EChartsCoreOption } from 'echarts/core'
import { api } from '@/api/client'
import type { Credential, HostKeyScanResult, MetricPoint, Server, ServerInput } from '@/api/types'
import ChartPanel from '@/components/ChartPanel.vue'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatBytes, formatDate, percent, serverStatus } from '@/utils/format'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const metricsLoading = ref(false)
const servers = ref<Server[]>([])
const credentials = ref<Credential[]>([])
const selectedId = ref<string | null>(null)
const metrics = ref<MetricPoint[]>([])
const showEditor = ref(false)
const saving = ref(false)
const scanningHostKey = ref(false)
const scannedHostKey = ref<HostKeyScanResult | null>(null)
const hostKeyConfirmed = ref(false)
const editingId = ref<string | null>(null)
const originalHostKey = ref<string | null>(null)
const endpointChanged = ref(false)
let hydratingForm = false
const formRef = ref<FormInst | null>(null)
const form = reactive<ServerInput>({
  name: '',
  host: '',
  port: 22,
  username: 'root',
  ssh_credential_id: null,
  host_key: null,
  labels: {},
  enabled: true,
})
const rules: FormRules = {
  name: { required: true, message: '请输入服务器名称', trigger: 'blur' },
  host: { required: true, message: '请输入主机地址', trigger: 'blur' },
  username: { required: true, message: '请输入 SSH 用户名', trigger: 'blur' },
}
const selected = computed(() => servers.value.find((item) => item.id === selectedId.value) || null)
const latest = computed(() => metrics.value.at(-1))

async function load(): Promise<void> {
  /** 加载服务器登记信息，并选择当前可用主机。 */
  loading.value = true
  try {
    const [serverList, credentialList] = await Promise.all([
      api.servers.list(),
      api.credentials.list(),
    ])
    servers.value = serverList
    credentials.value = credentialList.filter((item) => item.kind === 'ssh')
    if (!selectedId.value || !servers.value.some((item) => item.id === selectedId.value))
      selectedId.value = servers.value[0]?.id || null
  } catch (error) {
    message.error(error instanceof Error ? error.message : '服务器加载失败')
  } finally {
    loading.value = false
  }
}

function resetHostKeyState(preserveOriginal = false): void {
  /** 重置 SSH 指纹扫描状态，按需保留原已登记指纹。 */
  scannedHostKey.value = null
  hostKeyConfirmed.value = false
  form.host_key = preserveOriginal ? originalHostKey.value : null
}

function resetServerForm(): void {
  /** 清空服务器表单并恢复 SSH 默认端口和启用状态。 */
  editingId.value = null
  originalHostKey.value = null
  endpointChanged.value = false
  hydratingForm = true
  Object.assign(form, {
    name: '',
    host: '',
    port: 22,
    username: 'root',
    ssh_credential_id: null,
    host_key: null,
    labels: {},
    enabled: true,
  })
  hydratingForm = false
  resetHostKeyState()
  formRef.value?.restoreValidation()
}

function openCreate(): void {
  /** 打开新服务器登记表单。 */
  resetServerForm()
  showEditor.value = true
}

function openEdit(server: Server): void {
  /** 将服务器非敏感配置复制到编辑表单。 */
  editingId.value = server.id
  originalHostKey.value = server.host_key
  endpointChanged.value = false
  scannedHostKey.value = null
  hostKeyConfirmed.value = false
  hydratingForm = true
  Object.assign(form, {
    name: server.name,
    host: server.host,
    port: server.port,
    username: server.username,
    ssh_credential_id: server.ssh_credential_id,
    host_key: server.host_key,
    labels: { ...server.labels },
    enabled: server.enabled,
  })
  hydratingForm = false
  formRef.value?.restoreValidation()
  showEditor.value = true
}

async function scanHostKey(): Promise<void> {
  /** 扫描目标主机指纹，必须由管理员确认后才能保存。 */
  const host = form.host.trim()
  const port = form.port
  if (!host) {
    message.warning('请先填写主机地址')
    return
  }
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    message.warning('SSH 端口必须在 1 到 65535 之间')
    return
  }

  resetHostKeyState(Boolean(editingId.value && !endpointChanged.value))
  scanningHostKey.value = true
  try {
    const result = await api.servers.scanHostKey(host, port)
    if (form.host.trim() !== host || form.port !== port) return
    scannedHostKey.value = result
    message.info('已读取服务器当前主机密钥，请人工核对后确认')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '主机指纹扫描失败')
  } finally {
    scanningHostKey.value = false
  }
}

async function loadMetrics(): Promise<void> {
  /** 加载选中服务器最近 24 小时资源指标。 */
  if (!selectedId.value) return
  metricsLoading.value = true
  try {
    metrics.value = await api.servers.metrics(selectedId.value)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '指标加载失败')
    metrics.value = []
  } finally {
    metricsLoading.value = false
  }
}

async function save(): Promise<void> {
  /** 保存服务器配置，并要求启用主机必须绑定已确认 SSH 指纹。 */
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  const pinnedHostKey = hostKeyConfirmed.value
    ? scannedHostKey.value?.fingerprint || null
    : endpointChanged.value
      ? null
      : originalHostKey.value
  if (form.enabled && !pinnedHostKey) {
    message.warning('启用服务器前必须扫描并明确确认 SSH 主机指纹')
    return
  }

  const payload: ServerInput = {
    ...form,
    name: form.name.trim(),
    host: form.host.trim(),
    username: form.username.trim(),
    host_key: pinnedHostKey,
    labels: { ...form.labels },
  }
  saving.value = true
  try {
    const saved = editingId.value
      ? await api.servers.update(editingId.value, payload)
      : await api.servers.create(payload)
    message.success(
      editingId.value
        ? '服务器配置已更新'
        : payload.enabled
          ? '服务器已登记并固定主机指纹'
          : '服务器已停用登记',
    )
    showEditor.value = false
    await load()
    selectedId.value = saved.id
  } catch (error) {
    message.error(
      error instanceof Error
        ? error.message
        : editingId.value
          ? '服务器更新失败'
          : '服务器登记失败',
    )
  } finally {
    saving.value = false
  }
}

async function test(): Promise<void> {
  /** 通过 Runner 测试 SSH 和 Docker 连通性。 */
  if (!selected.value) return
  try {
    await api.servers.metrics(selected.value.id, 1)
    message.success('服务器 API 与指标查询正常')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '连接检查失败')
  }
}

function remove(server: Server): void {
  /** 在确认后删除服务器登记，后端负责检查环境引用。 */
  dialog.warning({
    title: '移除服务器',
    content: `只移除控制台登记，不会删除“${server.name}”上的容器和文件。确定继续？`,
    positiveText: '确认移除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.servers.remove(server.id)
        message.success('服务器已移除')
        selectedId.value = null
        await load()
      } catch (error) {
        message.error(error instanceof Error ? error.message : '移除失败')
      }
    },
  })
}

const metricsOption = computed<EChartsCoreOption>(() => ({
  animationDuration: 350,
  grid: { top: 30, right: 18, bottom: 28, left: 38 },
  tooltip: {
    trigger: 'axis',
    backgroundColor: '#121922',
    borderColor: '#283340',
    textStyle: { color: '#dce5ee' },
  },
  legend: {
    top: 0,
    right: 4,
    data: ['CPU', '内存'],
    textStyle: { color: '#73808f', fontSize: 10 },
  },
  xAxis: {
    type: 'category',
    data: metrics.value.map((item) => formatDate(item.collected_at)),
    axisLabel: { color: '#5e6b79', fontSize: 9, hideOverlap: true },
    axisLine: { lineStyle: { color: '#27313d' } },
    axisTick: { show: false },
  },
  yAxis: {
    type: 'value',
    min: 0,
    max: 100,
    axisLabel: { color: '#5e6b79', fontSize: 9, formatter: '{value}%' },
    splitLine: { lineStyle: { color: '#1c2530' } },
  },
  series: [
    {
      name: 'CPU',
      type: 'line',
      smooth: true,
      showSymbol: false,
      data: metrics.value.map((item) => item.cpu_percent),
      lineStyle: { color: '#c7ff4a', width: 2 },
      areaStyle: { color: 'rgba(199,255,74,.06)' },
    },
    {
      name: '内存',
      type: 'line',
      smooth: true,
      showSymbol: false,
      data: metrics.value.map((item) => percent(item.memory_used, item.memory_total)),
      lineStyle: { color: '#5ea1ff', width: 2 },
    },
  ],
}))

watch(selectedId, loadMetrics)
watch(
  [() => form.host, () => form.port],
  () => {
    if (hydratingForm || !showEditor.value) return
    endpointChanged.value = true
    resetHostKeyState()
  },
  { flush: 'sync' },
)
watch(hostKeyConfirmed, (confirmed) => {
  form.host_key = confirmed
    ? scannedHostKey.value?.fingerprint || null
    : editingId.value && !endpointChanged.value
      ? originalHostKey.value
      : null
})
onMounted(load)
</script>

<style scoped>
.server-layout {
  display: grid;
  grid-template-columns: 285px minmax(0, 1fr);
  gap: 16px;
}
.server-list {
  align-self: start;
  padding: 7px;
}
.server-list button {
  display: grid;
  width: 100%;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 11px;
  border: 1px solid transparent;
  border-radius: 10px;
  color: #8996a5;
  background: transparent;
  cursor: pointer;
  text-align: left;
}
.server-list button:hover {
  background: #121923;
}
.server-list button.active {
  border-color: #2b3743;
  background: #151d26;
}
.server-avatar {
  display: grid;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  color: #8f9baa;
  background: #0b1118;
  place-items: center;
}
.active .server-avatar {
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.07);
}
.server-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.server-copy b {
  overflow: hidden;
  color: #b9c4cf;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.server-copy small {
  overflow: hidden;
  margin-top: 3px;
  color: #5c6876;
  font-size: 8px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.server-main {
  display: grid;
  gap: 14px;
  min-width: 0;
}
.node-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 17px;
}
.node-head > div,
.node-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.node-icon {
  display: grid;
  width: 48px;
  height: 48px;
  border: 1px solid #293440;
  border-radius: 12px;
  color: #c7ff4a;
  background: #111923;
  place-items: center;
}
.node-head .eyebrow {
  margin-bottom: 3px;
}
.node-head h2 {
  margin: 0;
  font-size: 20px;
}
.endpoint {
  display: block;
  margin-top: 4px;
  color: #667381;
  font-size: 9px;
}
.resource-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.resource {
  padding: 16px;
}
.resource > div:first-child {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #7f8c9a;
  font-size: 10px;
}
.resource strong {
  display: block;
  margin: 12px 0 9px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 19px;
}
.resource small {
  display: block;
  margin-top: 8px;
  color: #5e6b79;
  font-size: 9px;
}
.net-row {
  display: flex;
  justify-content: space-between;
  margin: 12px 0 11px;
  color: #718090;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
}
.metrics-panel {
  padding: 20px;
}
.fingerprint {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 14px;
  color: #778494;
}
.fingerprint div {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}
.fingerprint b {
  color: #aeb9c5;
  font-size: 10px;
}
.fingerprint code {
  overflow: hidden;
  margin-top: 3px;
  color: #657280;
  font-size: 9px;
  text-overflow: ellipsis;
}
.fingerprint > span {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
}
.server-modal {
  width: min(700px, calc(100vw - 30px));
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.host-key-panel {
  display: grid;
  gap: 12px;
  margin-bottom: 18px;
  padding: 14px;
  border: 1px solid #27323e;
  border-radius: 11px;
  background: #0b1118;
}
.host-key-heading,
.host-key-heading > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.host-key-heading > div > span {
  display: grid;
  width: 36px;
  height: 36px;
  flex: 0 0 auto;
  border-radius: 9px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.07);
  place-items: center;
}
.host-key-heading > div > div {
  display: flex;
  flex-direction: column;
}
.host-key-heading b {
  font-size: 11px;
}
.host-key-heading small {
  margin-top: 3px;
  color: #657281;
  font-size: 9px;
}
.current-host-key {
  display: block;
  overflow: hidden;
  margin-top: 6px;
  color: #79dca8;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.host-key-result {
  display: grid;
  gap: 12px;
}
.host-key-result dl {
  display: grid;
  gap: 1px;
  margin: 0;
  overflow: hidden;
  border: 1px solid #25303b;
  border-radius: 9px;
}
.host-key-result dl > div {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: 12px;
  padding: 9px 11px;
  background: #101720;
}
.host-key-result dt {
  color: #677483;
  font-size: 9px;
}
.host-key-result dd {
  min-width: 0;
  margin: 0;
}
.host-key-result code {
  display: block;
  overflow: hidden;
  color: #aebbc8;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.host-key-result .public-key code {
  max-height: 64px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 13px;
  border: 1px solid #26313c;
  border-radius: 10px;
  background: #0c1219;
}
.switch-row > div {
  display: flex;
  flex-direction: column;
}
.switch-row strong {
  font-size: 11px;
}
.switch-row span {
  margin-top: 3px;
  color: #657281;
  font-size: 9px;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
@media (max-width: 1200px) {
  .server-layout {
    grid-template-columns: 1fr;
  }
  .server-list {
    display: flex;
    overflow-x: auto;
  }
  .server-list button {
    min-width: 250px;
  }
  .resource-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 650px) {
  .node-head {
    align-items: flex-start;
    flex-direction: column;
  }
  .node-actions {
    width: 100%;
    flex-wrap: wrap;
  }
  .resource-grid,
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
