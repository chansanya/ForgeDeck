<!-- 控制台总览：聚合资源状态、运行队列、部署健康度和关键指标。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="CONTROL PLANE"
      title="运行总览"
      description="构建、部署和基础设施的实时脉搏。关键异常应该在这里一眼看见。"
    >
      <template #actions
        ><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ></template
      >
    </PageHeader>
    <div v-if="loading && !summary" class="loading-block"><NSpin size="large" /></div>
    <template v-else-if="summary">
      <section class="metric-grid">
        <MetricCard
          label="活跃流水线"
          :value="activeRuns"
          :caption="`${summary.queued_runs} 个排队中`"
          :icon="Activity"
        />
        <MetricCard
          label="项目总数"
          :value="summary.project_count"
          caption="已登记源码仓库"
          :icon="Boxes"
          tone="blue"
        />
        <MetricCard
          label="失败运行"
          :value="summary.failed_runs"
          caption="累计失败状态"
          :icon="CircleAlert"
          tone="red"
        />
        <MetricCard
          label="在线节点"
          :value="`${onlineServers}/${summary.server_count}`"
          caption="最近两分钟有心跳"
          :icon="ServerIcon"
          tone="amber"
        />
      </section>
      <section class="overview-grid">
        <article class="panel chart-panel">
          <div class="section-heading">
            <div>
              <p class="eyebrow">THROUGHPUT</p>
              <h2>运行趋势</h2>
            </div>
            <span class="range-label">7 DAYS</span>
          </div>
          <ChartPanel :option="chartOption" :height="250" />
        </article>
        <article class="panel signal-panel">
          <div class="section-heading">
            <div>
              <p class="eyebrow">SIGNALS</p>
              <h2>需要关注</h2>
            </div>
          </div>
          <div class="signal-list">
            <button type="button" @click="router.push({ name: 'approvals' })">
              <span class="signal-icon amber"><Clock3 :size="17" /></span
              ><span
                ><b>{{ summary.pending_approvals }} 项待审批</b
                ><small>部署、回滚或脚本执行</small></span
              ><ArrowRight :size="15" />
            </button>
            <button type="button" @click="router.push({ name: 'deployments' })">
              <span class="signal-icon lime"><Boxes :size="17" /></span
              ><span
                ><b>{{ healthyDeployments }} 个健康部署</b><small>当前环境修订版本</small></span
              ><ArrowRight :size="15" />
            </button>
            <button type="button" @click="router.push({ name: 'servers' })">
              <span class="signal-icon blue"><ServerIcon :size="17" /></span
              ><span
                ><b>{{ onlineServers }} 台服务器在线</b><small>指标采集正常</small></span
              ><ArrowRight :size="15" />
            </button>
          </div>
        </article>
      </section>
      <section class="panel runs-panel">
        <div class="section-heading">
          <div>
            <p class="eyebrow">LATEST EXECUTIONS</p>
            <h2>最近运行</h2>
          </div>
          <NButton text type="primary" @click="router.push({ name: 'pipelines' })"
            >查看全部 <ArrowRight :size="14"
          /></NButton>
        </div>
        <NDataTable
          :columns="columns"
          :data="recentRuns"
          :bordered="false"
          :single-line="false"
          :scroll-x="900"
        />
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NDataTable, NIcon, NSpin, useMessage, type DataTableColumns } from 'naive-ui'
import {
  Activity,
  ArrowRight,
  Boxes,
  CircleAlert,
  Clock3,
  RefreshCw,
  Server as ServerIcon,
} from 'lucide-vue-next'
import type { EChartsCoreOption } from 'echarts/core'
import { api } from '@/api/client'
import type { DashboardSummary, Deployment, PipelineRun, Project, Server } from '@/api/types'
import ChartPanel from '@/components/ChartPanel.vue'
import MetricCard from '@/components/MetricCard.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { durationSeconds, formatDate, formatDuration, serverStatus, shortSha } from '@/utils/format'

const router = useRouter()
const message = useMessage()
const loading = ref(true)
const summary = ref<DashboardSummary | null>(null)
const runs = ref<PipelineRun[]>([])
const deployments = ref<Deployment[]>([])
const servers = ref<Server[]>([])
const projects = ref<Project[]>([])

async function load(): Promise<void> {
  /** 并行加载总览统计、近期运行和主机数据，失败时保留可用部分。 */
  loading.value = true
  try {
    ;[summary.value, runs.value, deployments.value, servers.value, projects.value] =
      await Promise.all([
        api.dashboard.summary(),
        api.runs.list({ limit: 50 }),
        api.deployments.list(),
        api.servers.list(),
        api.projects.list(),
      ])
  } catch (error) {
    message.error(error instanceof Error ? error.message : '总览数据加载失败')
  } finally {
    loading.value = false
  }
}

const activeRuns = computed(
  () => (summary.value?.queued_runs || 0) + (summary.value?.running_runs || 0),
)
const healthyDeployments = computed(
  () => deployments.value.filter((item) => item.status === 'healthy').length,
)
const onlineServers = computed(
  () => servers.value.filter((item) => serverStatus(item) === 'online').length,
)
const recentRuns = computed(() => runs.value.slice(0, 8))

const chartOption = computed<EChartsCoreOption>(() => {
  const buckets = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(Date.now() - (6 - index) * 86400000)
    return {
      key: date.toISOString().slice(0, 10),
      label: `${date.getMonth() + 1}/${date.getDate()}`,
      succeeded: 0,
      failed: 0,
    }
  })
  for (const run of runs.value) {
    const bucket = buckets.find((item) => item.key === run.created_at.slice(0, 10))
    if (!bucket) continue
    if (run.status === 'succeeded') bucket.succeeded += 1
    if (run.status === 'failed') bucket.failed += 1
  }
  return {
    animationDuration: 450,
    grid: { top: 28, right: 14, bottom: 26, left: 36 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#121922',
      borderColor: '#283340',
      textStyle: { color: '#dce5ee' },
    },
    legend: {
      top: 0,
      right: 4,
      textStyle: { color: '#778493', fontSize: 10 },
      data: ['成功', '失败'],
    },
    xAxis: {
      type: 'category',
      data: buckets.map((item) => item.label),
      axisLine: { lineStyle: { color: '#27313d' } },
      axisLabel: { color: '#667382', fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: '#667382', fontSize: 10 },
      splitLine: { lineStyle: { color: '#1d2630' } },
    },
    series: [
      {
        name: '成功',
        type: 'line',
        smooth: true,
        symbolSize: 6,
        data: buckets.map((item) => item.succeeded),
        lineStyle: { color: '#c7ff4a', width: 2 },
        itemStyle: { color: '#c7ff4a' },
        areaStyle: { color: 'rgba(199,255,74,.07)' },
      },
      {
        name: '失败',
        type: 'line',
        smooth: true,
        symbolSize: 6,
        data: buckets.map((item) => item.failed),
        lineStyle: { color: '#ff637d', width: 2 },
        itemStyle: { color: '#ff637d' },
      },
    ],
  }
})

const columns: DataTableColumns<PipelineRun> = [
  {
    title: '项目 / 提交',
    key: 'project',
    minWidth: 190,
    render: (row) =>
      h('div', { class: 'run-primary' }, [
        h(
          'strong',
          projects.value.find((item) => item.id === row.project_id)?.name || row.project_id,
        ),
        h('span', { class: 'mono' }, `${row.ref} · ${shortSha(row.commit_sha)}`),
      ]),
  },
  {
    title: '触发',
    key: 'trigger',
    width: 100,
    render: (row) => h('span', { class: 'mono muted' }, row.trigger_type.toUpperCase()),
  },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render: (row) => h(StatusBadge, { status: row.status }),
  },
  { title: '阶段', key: 'stage', minWidth: 120, render: (row) => row.current_stage || '—' },
  {
    title: '耗时',
    key: 'duration',
    width: 90,
    render: (row) => formatDuration(durationSeconds(row.started_at, row.finished_at)),
  },
  { title: '时间', key: 'created', width: 130, render: (row) => formatDate(row.created_at) },
  {
    title: '',
    key: 'actions',
    width: 52,
    render: (row) =>
      h(
        NButton,
        {
          quaternary: true,
          circle: true,
          'aria-label': '查看运行',
          onClick: () => router.push({ name: 'run-detail', params: { id: row.id } }),
        },
        { icon: () => h(NIcon, { component: ArrowRight }) },
      ),
  },
]

onMounted(load)
</script>

<style scoped>
.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}
.overview-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.65fr) minmax(300px, 0.75fr);
  gap: 16px;
  margin-bottom: 16px;
}
.chart-panel,
.signal-panel,
.runs-panel {
  padding: 20px;
}
.range-label {
  color: #55616f;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.12em;
}
.signal-list {
  display: grid;
  gap: 9px;
}
.signal-list button {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 11px;
  width: 100%;
  padding: 12px;
  border: 1px solid #202a35;
  border-radius: 11px;
  color: #8491a0;
  background: #0c1219;
  cursor: pointer;
  text-align: left;
}
.signal-list button:hover {
  border-color: #344250;
  transform: translateX(2px);
}
.signal-icon {
  display: grid;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  place-items: center;
}
.signal-icon.amber {
  color: #f5b942;
  background: rgba(245, 185, 66, 0.09);
}
.signal-icon.lime {
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.08);
}
.signal-icon.blue {
  color: #5ea1ff;
  background: rgba(94, 161, 255, 0.09);
}
.signal-list span:nth-child(2) {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.signal-list b {
  color: #bec9d5;
  font-size: 12px;
}
.signal-list small {
  margin-top: 3px;
  color: #626f7e;
  font-size: 10px;
}
:deep(.run-primary) {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
:deep(.run-primary strong) {
  font-size: 12px;
}
:deep(.run-primary span) {
  color: #6d7988;
  font-size: 10px;
}
@media (max-width: 1200px) {
  .metric-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .overview-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 620px) {
  .metric-grid {
    grid-template-columns: 1fr;
  }
}
</style>
