<!-- 审计页面：按时间展示认证、配置和操作申请的追加式记录。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="IMMUTABLE RECORD"
      title="审计日志"
      description="登录、配置变更、审批和危险操作都在这里留下追加式元数据。日志不是装饰品，是出事后还能讲清楚的底线。"
    >
      <template #actions
        ><NSelect
          v-model:value="result"
          class="result-select"
          clearable
          placeholder="全部结果"
          :options="[
            { label: '成功', value: 'success' },
            { label: '失败', value: 'failure' },
            { label: '拒绝', value: 'denied' },
          ]"
          @update:value="load"
        /><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ></template
      >
    </PageHeader>
    <section class="panel table-panel">
      <NDataTable
        v-if="events.length || loading"
        :columns="columns"
        :data="events"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :scroll-x="1050"
      /><EmptyState
        v-else
        :icon="History"
        title="没有审计记录"
        description="当前筛选条件下没有事件。"
      />
    </section>
    <NDrawer
      :show="Boolean(selected)"
      placement="right"
      :width="480"
      @update:show="
        (value) => {
          if (!value) selected = null
        }
      "
      ><NDrawerContent title="审计事件详情" closable
        ><template v-if="selected"
          ><dl>
            <dt>事件 ID</dt>
            <dd class="mono">{{ selected.id }}</dd>
            <dt>时间</dt>
            <dd>{{ formatFullDate(selected.created_at) }}</dd>
            <dt>操作者</dt>
            <dd>{{ selected.actor }}</dd>
            <dt>来源</dt>
            <dd class="mono">{{ selected.source_ip || '—' }}</dd>
            <dt>操作</dt>
            <dd class="mono">{{ selected.action }}</dd>
            <dt>资源</dt>
            <dd>{{ selected.resource_type }} / {{ selected.resource_id || '—' }}</dd>
            <dt>结果</dt>
            <dd>
              <StatusBadge :status="selected.outcome === 'failure' ? 'failed' : selected.outcome" />
            </dd>
          </dl>
          <h3>详情</h3>
          <NCode
            :code="JSON.stringify(selected.details || {}, null, 2)"
            language="json"
            word-wrap /></template></NDrawerContent
    ></NDrawer>
  </div>
</template>

<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import {
  NButton,
  NCode,
  NDataTable,
  NDrawer,
  NDrawerContent,
  NIcon,
  NSelect,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import { Eye, History, RefreshCw } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { AuditEvent } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatFullDate } from '@/utils/format'

const message = useMessage()
const loading = ref(false)
const events = ref<AuditEvent[]>([])
const result = ref<string | null>(null)
const selected = ref<AuditEvent | null>(null)

async function load(): Promise<void> {
  /** 读取审计元数据列表，不请求或展示任何凭据明文。 */
  loading.value = true
  try {
    events.value = await api.audit.list({ outcome: result.value || undefined, limit: 200 })
  } catch (error) {
    message.error(error instanceof Error ? error.message : '审计日志加载失败')
  } finally {
    loading.value = false
  }
}
const columns: DataTableColumns<AuditEvent> = [
  {
    title: '时间',
    key: 'created_at',
    width: 180,
    render: (row) => h('span', { class: 'mono audit-time' }, formatFullDate(row.created_at)),
  },
  { title: '操作者', key: 'actor', width: 140 },
  {
    title: '操作',
    key: 'action',
    minWidth: 180,
    render: (row) => h('code', { class: 'action-code' }, row.action),
  },
  {
    title: '资源',
    key: 'resource',
    minWidth: 210,
    render: (row) =>
      h('div', { class: 'resource-cell' }, [
        h('strong', row.resource_id || '—'),
        h('small', row.resource_type),
      ]),
  },
  {
    title: '来源 IP',
    key: 'source_ip',
    width: 140,
    render: (row) => h('span', { class: 'mono muted' }, row.source_ip || '—'),
  },
  {
    title: '结果',
    key: 'result',
    width: 105,
    render: (row) => h(StatusBadge, { status: row.outcome === 'failure' ? 'failed' : row.outcome }),
  },
  {
    title: '',
    key: 'details',
    width: 54,
    render: (row) =>
      h(
        NButton,
        {
          quaternary: true,
          circle: true,
          onClick: () => {
            selected.value = row
          },
        },
        { icon: () => h(NIcon, { component: Eye }) },
      ),
  },
]
onMounted(load)
</script>

<style scoped>
.result-select {
  width: 140px;
}
.table-panel {
  padding-top: 4px;
}
:deep(.audit-time) {
  color: #718090;
  font-size: 9px;
}
:deep(.action-code) {
  color: #c7ff4a;
  font-size: 9px;
}
:deep(.resource-cell) {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
:deep(.resource-cell strong) {
  font-size: 11px;
}
:deep(.resource-cell small) {
  color: #62707f;
  font-size: 9px;
}
dl {
  display: grid;
  grid-template-columns: 100px 1fr;
  gap: 13px;
  margin: 0;
}
dt {
  color: #687584;
  font-size: 10px;
}
dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  color: #aeb9c5;
  font-size: 11px;
}
h3 {
  margin: 26px 0 10px;
  font-size: 13px;
}
</style>
