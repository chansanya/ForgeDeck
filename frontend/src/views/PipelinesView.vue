<!-- 流水线页面：筛选并查看构建部署运行记录和当前状态。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="PIPELINE EXECUTIONS"
      title="流水线"
      description="每次运行绑定确定的 commit、配置快照与镜像 digest，失败不会被一句“偶现”糊弄过去。"
    >
      <template #actions
        ><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ></template
      >
    </PageHeader>
    <section class="filters panel">
      <div>
        <label>项目</label
        ><NSelect
          v-model:value="projectId"
          clearable
          placeholder="全部项目"
          :options="projects.map((p) => ({ label: p.name, value: p.id }))"
          @update:value="load"
        />
      </div>
      <div>
        <label>状态</label
        ><NSelect
          v-model:value="status"
          clearable
          placeholder="全部状态"
          :options="[
            { label: '排队中', value: 'queued' },
            { label: '运行中', value: 'running' },
            { label: '成功', value: 'succeeded' },
            { label: '失败', value: 'failed' },
            { label: '已取消', value: 'cancelled' },
          ]"
          @update:value="load"
        />
      </div>
      <span class="result-count mono">{{ runs.length }} RUNS</span>
    </section>
    <section class="panel table-panel">
      <NDataTable
        v-if="runs.length || loading"
        :data="runs"
        :columns="columns"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :scroll-x="1120"
      />
      <EmptyState
        v-else
        :icon="Workflow"
        title="没有符合条件的运行"
        description="调整筛选条件，或从项目页手动触发一次流水线。"
      />
    </section>
  </div>
</template>

<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NDataTable, NIcon, NSelect, useMessage, type DataTableColumns } from 'naive-ui'
import { ArrowRight, GitCommitHorizontal, RefreshCw, Workflow } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { PipelineRun, Project, RunStatus } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { durationSeconds, formatDate, formatDuration, shortSha } from '@/utils/format'

const router = useRouter()
const message = useMessage()
const runs = ref<PipelineRun[]>([])
const projects = ref<Project[]>([])
const loading = ref(false)
const status = ref<RunStatus | null>(null)
const projectId = ref<string | null>(null)

async function load(): Promise<void> {
  /** 加载流水线运行列表并应用当前筛选条件。 */
  loading.value = true
  try {
    const [runList, projectList] = await Promise.all([
      api.runs.list({
        status: status.value || undefined,
        project_id: projectId.value || undefined,
        limit: 100,
      }),
      api.projects.list(),
    ])
    runs.value = runList
    projects.value = projectList
  } catch (error) {
    message.error(error instanceof Error ? error.message : '流水线加载失败')
  } finally {
    loading.value = false
  }
}

const columns: DataTableColumns<PipelineRun> = [
  {
    title: '运行',
    key: 'id',
    width: 110,
    render: (row) => h('span', { class: 'mono run-id' }, `#${row.id.slice(0, 8)}`),
  },
  {
    title: '项目',
    key: 'project',
    minWidth: 170,
    render: (row) =>
      h('div', { class: 'run-project' }, [
        h('strong', projects.value.find((p) => p.id === row.project_id)?.name || row.project_id),
        h('small', row.environment_id || '仅构建'),
      ]),
  },
  {
    title: '提交',
    key: 'commit',
    minWidth: 170,
    render: (row) =>
      h('div', { class: 'commit-cell' }, [
        h('span', [h(GitCommitHorizontal, { size: 13 }), shortSha(row.commit_sha)]),
        h('small', row.ref),
      ]),
  },
  {
    title: '触发',
    key: 'trigger',
    width: 100,
    render: (row) => h('span', { class: 'mono trigger' }, row.trigger_type.toUpperCase()),
  },
  {
    title: '状态',
    key: 'status',
    width: 112,
    render: (row) => h(StatusBadge, { status: row.status }),
  },
  {
    title: '当前阶段',
    key: 'current_stage',
    minWidth: 130,
    render: (row) => row.current_stage || '—',
  },
  {
    title: '耗时',
    key: 'duration',
    width: 90,
    render: (row) => formatDuration(durationSeconds(row.started_at, row.finished_at)),
  },
  { title: '创建时间', key: 'created_at', width: 140, render: (row) => formatDate(row.created_at) },
  {
    title: '',
    key: 'action',
    width: 54,
    render: (row) =>
      h(
        NButton,
        {
          circle: true,
          quaternary: true,
          onClick: () => router.push({ name: 'run-detail', params: { id: row.id } }),
        },
        { icon: () => h(NIcon, { component: ArrowRight }) },
      ),
  },
]

onMounted(load)
</script>

<style scoped>
.filters {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  margin-bottom: 16px;
  padding: 14px;
}
.filters > div {
  width: 210px;
}
.filters label {
  display: block;
  margin: 0 0 6px 2px;
  color: #6f7c8b;
  font-size: 10px;
}
.result-count {
  margin-left: auto;
  padding: 8px 10px;
  color: #657281;
  font-size: 9px;
  letter-spacing: 0.09em;
}
.table-panel {
  padding-top: 4px;
}
:deep(.run-id) {
  color: #c7ff4a;
  font-size: 10px;
}
:deep(.run-project),
:deep(.commit-cell) {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
:deep(.run-project strong) {
  font-size: 12px;
}
:deep(.run-project small),
:deep(.commit-cell small) {
  overflow: hidden;
  max-width: 190px;
  color: #687584;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.commit-cell span) {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #9ba8b6;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
}
:deep(.trigger) {
  color: #6e7b8a;
  font-size: 9px;
}
@media (max-width: 650px) {
  .filters {
    align-items: stretch;
    flex-direction: column;
  }
  .filters > div {
    width: 100%;
  }
  .result-count {
    margin-left: 0;
  }
}
</style>
