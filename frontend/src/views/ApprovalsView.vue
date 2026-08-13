<!-- 审批中心：复核参数哈希并批准或拒绝高权限操作申请。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="CHANGE CONTROL"
      title="审批中心"
      description="AI 和管理员发起的写操作必须绑定参数哈希。批准的是这组参数，不是给未来任何请求开绿灯。"
    >
      <template #actions
        ><NSelect
          v-model:value="status"
          class="status-select"
          clearable
          placeholder="全部状态"
          :options="[
            { label: '待审批', value: 'pending' },
            { label: '已批准', value: 'approved' },
            { label: '已拒绝', value: 'rejected' },
            { label: '已过期', value: 'expired' },
          ]"
          @update:value="load"
        /><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ></template
      >
    </PageHeader>
    <section class="panel table-panel">
      <div class="approval-banner">
        <Clock3 :size="16" /><span
          >审批前请核对资源、原因与参数哈希。对看不懂的变更点“批准”，和闭眼签字没区别。</span
        >
      </div>
      <NDataTable
        v-if="approvals.length || loading"
        :columns="columns"
        :data="approvals"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :scroll-x="1150"
      /><EmptyState
        v-else
        :icon="ShieldCheck"
        title="没有待处理审批"
        description="当前筛选条件下没有变更申请。"
      />
    </section>
    <NModal
      v-model:show="showReject"
      preset="card"
      title="拒绝申请"
      class="reject-modal"
      :bordered="false"
      ><p class="reject-target">
        {{ rejectTarget?.kind }} · {{ JSON.stringify(rejectTarget?.preview || {}) }}
      </p>
      <p class="muted">拒绝请求会绑定同一参数哈希并写入审计日志。</p>
      <template #footer
        ><div class="modal-footer">
          <NButton @click="showReject = false">取消</NButton
          ><NButton type="error" :loading="deciding" @click="reject">确认拒绝</NButton>
        </div></template
      ></NModal
    >
  </div>
</template>

<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import {
  NButton,
  NDataTable,
  NIcon,
  NModal,
  NSelect,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import { Check, Clock3, Fingerprint, RefreshCw, ShieldCheck, X } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Approval } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatFullDate } from '@/utils/format'

const message = useMessage()
const dialog = useDialog()
const approvals = ref<Approval[]>([])
const loading = ref(false)
const status = ref<string | null>('pending')
const rejectTarget = ref<Approval | null>(null)
const deciding = ref(false)

async function load(): Promise<void> {
  /** 加载待审批操作及其参数哈希和影响预览。 */
  loading.value = true
  try {
    approvals.value = await api.approvals.list(status.value || undefined)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '审批加载失败')
  } finally {
    loading.value = false
  }
}
function approve(row: Approval): void {
  /** 打开审批确认对话框，实际提交仍携带原参数哈希。 */
  dialog.warning({
    title: '确认批准操作',
    content: () =>
      h('div', { class: 'approval-confirm' }, [
        h('p', `批准后将执行：${row.kind}`),
        h('pre', JSON.stringify(row.preview, null, 2)),
        h('code', row.parameter_hash),
      ]),
    positiveText: '确认哈希并批准',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.approvals.approve(row.id, row.parameter_hash)
        message.success('已批准，任务将进入执行队列')
        await load()
      } catch (error) {
        message.error(error instanceof Error ? error.message : '批准失败')
      }
    },
  })
}
function openReject(row: Approval): void {
  /** 打开拒绝对话框并锁定当前申请的哈希。 */
  rejectTarget.value = row
  showReject.value = true
}
const showReject = ref(false)
async function reject(): Promise<void> {
  /** 提交拒绝申请并刷新审批列表。 */
  if (!rejectTarget.value) return
  deciding.value = true
  try {
    await api.approvals.reject(rejectTarget.value.id, rejectTarget.value.parameter_hash)
    message.success('申请已拒绝')
    showReject.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '拒绝失败')
  } finally {
    deciding.value = false
  }
}

const columns: DataTableColumns<Approval> = [
  {
    title: '申请操作',
    key: 'action',
    minWidth: 190,
    render: (row) =>
      h('div', { class: 'action-cell' }, [
        h('span', [h(ShieldCheck, { size: 17 })]),
        h('div', [
          h('strong', row.kind.toUpperCase()),
          h('small', String(row.preview.action || row.preview.target || '受控操作')),
        ]),
      ]),
  },
  { title: '申请人', key: 'requester', width: 130, render: (row) => row.requested_by },
  {
    title: '参数哈希',
    key: 'parameter_hash',
    minWidth: 210,
    render: (row) =>
      h('span', { class: 'hash mono' }, [
        h(Fingerprint, { size: 12 }),
        row.parameter_hash.slice(0, 24),
      ]),
  },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render: (row) => h(StatusBadge, { status: row.state }),
  },
  {
    title: '申请时间',
    key: 'requested_at',
    width: 170,
    render: (row) => formatFullDate(row.created_at),
  },
  {
    title: '过期时间',
    key: 'expires_at',
    width: 170,
    render: (row) => formatFullDate(row.expires_at),
  },
  {
    title: '',
    key: 'actions',
    width: 150,
    fixed: 'right',
    render: (row) =>
      row.state === 'pending'
        ? h('div', { class: 'decision-actions' }, [
            h(
              NButton,
              { size: 'small', type: 'success', secondary: true, onClick: () => approve(row) },
              { icon: () => h(NIcon, { component: Check }), default: () => '批准' },
            ),
            h(
              NButton,
              { size: 'small', type: 'error', secondary: true, onClick: () => openReject(row) },
              { icon: () => h(NIcon, { component: X }), default: () => '拒绝' },
            ),
          ])
        : null,
  },
]
onMounted(load)
</script>

<style scoped>
.status-select {
  width: 150px;
}
.table-panel {
  padding-top: 0;
}
.approval-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 17px;
  border-bottom: 1px solid #222b36;
  color: #8a7545;
  background: rgba(245, 185, 66, 0.035);
  font-size: 10px;
}
.approval-banner svg {
  color: #f5b942;
}
:deep(.action-cell) {
  display: flex;
  align-items: center;
  gap: 10px;
}
:deep(.action-cell > span) {
  display: grid;
  width: 35px;
  height: 35px;
  border-radius: 9px;
  color: #f5b942;
  background: rgba(245, 185, 66, 0.08);
  place-items: center;
}
:deep(.action-cell > div) {
  display: flex;
  flex-direction: column;
}
:deep(.action-cell strong) {
  font-size: 11px;
}
:deep(.action-cell small) {
  margin-top: 3px;
  color: #667382;
  font-size: 9px;
}
:deep(.hash) {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #84919f;
  font-size: 9px;
}
:deep(.decision-actions) {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.reject-modal {
  width: min(500px, calc(100vw - 30px));
}
.reject-target {
  color: #9ca9b7;
  font-size: 12px;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
:global(.approval-confirm code) {
  display: block;
  overflow: hidden;
  margin-top: 10px;
  padding: 9px;
  border-radius: 7px;
  color: #c7ff4a;
  background: #080c11;
  font-size: 9px;
  text-overflow: ellipsis;
}
</style>
