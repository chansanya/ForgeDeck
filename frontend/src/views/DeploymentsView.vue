<!-- 部署页面：创建部署申请、查看版本历史并发起精确回滚。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="RELEASE MANAGEMENT"
      title="部署"
      description="镜像 digest、环境和目标节点共同构成一次不可混淆的发布修订。健康检查失败时自动恢复上一版本。"
    >
      <template #actions
        ><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ><NButton type="primary" @click="showRequest = true"
          ><template #icon><NIcon :component="Plus" /></template>申请部署</NButton
        ></template
      >
    </PageHeader>
    <section class="panel table-panel">
      <div class="release-note">
        <ShieldCheck :size="15" /><span
          >生产变更受审批参数哈希保护；批准后参数被篡改将自动失效。</span
        >
      </div>
      <NDataTable
        v-if="deployments.length || loading"
        :data="deployments"
        :columns="columns"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :scroll-x="1050"
      /><EmptyState
        v-else
        :icon="Boxes"
        title="还没有部署记录"
        description="先完成一次成功构建，再将确定的镜像 digest 发布到目标环境。"
      />
    </section>
    <NModal
      v-model:show="showRequest"
      preset="card"
      title="申请部署"
      class="deploy-modal"
      :bordered="false"
    >
      <NForm ref="formRef" :model="form" :rules="rules" label-placement="top"
        ><NFormItem label="项目" path="project_id"
          ><NSelect
            v-model:value="form.project_id"
            placeholder="选择项目"
            :options="projects.map((p) => ({ label: p.name, value: p.id }))" /></NFormItem
        ><NFormItem label="目标环境" path="environment_id"
          ><NSelect
            v-model:value="form.environment_id"
            :disabled="!form.project_id"
            placeholder="选择已绑定服务器的环境"
            :options="environments.map((e) => ({ label: e.name, value: e.id }))" /></NFormItem
        ><NFormItem label="构建产物" path="run_id"
          ><NSelect
            v-model:value="form.run_id"
            placeholder="选择带镜像摘要的成功构建"
            :options="
              runs
                .filter((r) => r.project_id === form.project_id && r.image_digest)
                .map((r) => ({
                  label: `${shortSha(r.commit_sha)} · ${formatDate(r.created_at)}`,
                  value: r.id,
                }))
            " /></NFormItem
        ><NFormItem label="Compose 固化快照（仓库来源环境必填）" path="compose_content"
          ><NInput
            v-model:value="form.compose_content"
            type="textarea"
            :rows="5"
            class="mono"
            placeholder="services: ..." /></NFormItem
      ></NForm>
      <template #footer
        ><div class="modal-footer">
          <NButton @click="showRequest = false">取消</NButton
          ><NButton type="primary" :loading="saving" @click="requestDeploy">提交申请</NButton>
        </div></template
      >
    </NModal>
  </div>
</template>

<script setup lang="ts">
import { h, onMounted, reactive, ref, watch } from 'vue'
import {
  NButton,
  NDataTable,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NModal,
  NSelect,
  useDialog,
  useMessage,
  type DataTableColumns,
  type FormInst,
  type FormRules,
} from 'naive-ui'
import { Boxes, Plus, RefreshCw, RotateCcw, Server, ShieldCheck } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Deployment, Environment, PipelineRun, Project } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatDate, shortSha } from '@/utils/format'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const deployments = ref<Deployment[]>([])
const projects = ref<Project[]>([])
const runs = ref<PipelineRun[]>([])
const environments = ref<Environment[]>([])
const showRequest = ref(false)
const saving = ref(false)
const formRef = ref<FormInst | null>(null)
const form = reactive({
  project_id: '',
  environment_id: '',
  run_id: null as string | null,
  compose_content: '',
})
const rules: FormRules = {
  project_id: { required: true, message: '请选择项目' },
  environment_id: { required: true, message: '请选择环境' },
}

async function load(): Promise<void> {
  /** 加载部署记录和可用于部署的构建运行。 */
  loading.value = true
  try {
    ;[deployments.value, projects.value, runs.value] = await Promise.all([
      api.deployments.list(),
      api.projects.list(),
      api.runs.list({ status: 'succeeded', limit: 100 }),
    ])
  } catch (error) {
    message.error(error instanceof Error ? error.message : '部署记录加载失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => form.project_id,
  async (id) => {
    form.environment_id = ''
    form.run_id = null
    environments.value = []
    if (!id) return
    try {
      environments.value = await api.projects.environments(id)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '环境加载失败')
    }
  },
)

async function requestDeploy(): Promise<void> {
  /** 创建绑定镜像 digest 与 Compose 快照的部署审批申请。 */
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  saving.value = true
  const selectedRun = runs.value.find((item) => item.id === form.run_id)
  if (!selectedRun?.image_ref || !selectedRun.image_digest) {
    message.warning('请选择包含不可变镜像摘要的成功构建')
    saving.value = false
    return
  }
  try {
    const result = await api.deployments.request({
      environment_id: form.environment_id,
      image_ref: selectedRun.image_ref,
      image_digest: selectedRun.image_digest,
      revision: selectedRun.commit_sha,
      compose_content: form.compose_content || undefined,
    })
    message.success(
      'state' in result && result.state === 'pending' ? '部署申请已进入审批中心' : '部署任务已创建',
    )
    showRequest.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '部署申请失败')
  } finally {
    saving.value = false
  }
}

function rollback(row: Deployment): void {
  /** 针对指定部署打开回滚确认，不允许页面自行猜测上一 revision。 */
  dialog.warning({
    title: '申请回滚',
    content: `将“${projects.value.find((item) => item.id === row.project_id)?.name || row.project_id} / ${row.environment_id}”恢复到上一修订版本。该操作需要审批。`,
    positiveText: '提交回滚申请',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.deployments.rollback(row.id)
        message.success('回滚申请已提交')
      } catch (error) {
        message.error(error instanceof Error ? error.message : '回滚申请失败')
      }
    },
  })
}

const columns: DataTableColumns<Deployment> = [
  {
    title: '应用 / 环境',
    key: 'project',
    minWidth: 200,
    render: (row) =>
      h('div', { class: 'deploy-target' }, [
        h(
          'strong',
          projects.value.find((item) => item.id === row.project_id)?.name || row.project_id,
        ),
        h('small', row.environment_id),
      ]),
  },
  {
    title: '节点',
    key: 'server',
    minWidth: 150,
    render: (row) => h('span', { class: 'server-cell' }, [h(Server, { size: 13 }), row.server_id]),
  },
  {
    title: '修订',
    key: 'revision',
    minWidth: 140,
    render: (row) =>
      h('span', { class: 'mono revision' }, row.revision || shortSha(row.image_digest)),
  },
  {
    title: '镜像摘要',
    key: 'digest',
    minWidth: 170,
    render: (row) =>
      h('span', { class: 'mono digest' }, shortSha(row.image_digest.replace('sha256:', ''))),
  },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render: (row) => h(StatusBadge, { status: row.status }),
  },
  {
    title: '部署时间',
    key: 'created_at',
    width: 140,
    render: (row) => formatDate(row.started_at || row.created_at),
  },
  {
    title: '',
    key: 'actions',
    width: 90,
    render: (row) =>
      h(
        NButton,
        {
          size: 'small',
          secondary: true,
          disabled: row.status !== 'healthy',
          onClick: () => rollback(row),
        },
        { icon: () => h(NIcon, { component: RotateCcw }), default: () => '回滚' },
      ),
  },
]

onMounted(load)
</script>

<style scoped>
.table-panel {
  padding-top: 0;
}
.release-note {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 17px;
  border-bottom: 1px solid #222b36;
  color: #778493;
  background: rgba(94, 161, 255, 0.025);
  font-size: 10px;
}
.release-note svg {
  color: #5ea1ff;
}
:deep(.deploy-target) {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
:deep(.deploy-target strong) {
  font-size: 12px;
}
:deep(.deploy-target small) {
  color: #687584;
  font-size: 10px;
}
:deep(.server-cell) {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #8c99a8;
  font-size: 11px;
}
:deep(.revision) {
  color: #b5c1cd;
  font-size: 10px;
}
:deep(.digest) {
  color: #c7ff4a;
  font-size: 10px;
}
.deploy-modal {
  width: min(560px, calc(100vw - 30px));
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
</style>
