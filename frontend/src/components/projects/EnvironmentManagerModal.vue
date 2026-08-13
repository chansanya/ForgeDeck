<!-- 环境管理弹窗：绑定目标服务器并编辑 Compose、部署目录和健康检查。 -->
<template>
  <NModal
    :show="show"
    preset="card"
    class="environment-modal"
    :bordered="false"
    @update:show="emit('update:show', $event)"
  >
    <template #header
      ><div class="modal-heading">
        <span><Layers3 :size="19" /></span>
        <div>
          <p class="eyebrow">DEPLOY TARGETS</p>
          <h2>{{ project?.name }} · 部署环境</h2>
        </div>
      </div></template
    >
    <NAlert v-if="!servers.length" type="warning" :bordered="false" class="environment-alert"
      >请先登记服务器，才能创建部署环境。</NAlert
    >
    <section class="webhook-config">
      <div class="automation-heading">
        <GitBranch :size="17" />
        <div>
          <b>Webhook 自动部署默认值</b
          ><small>事件验签并固化 commit 后，流水线按这里选择环境和 Compose 服务。</small>
        </div>
      </div>
      <NForm label-placement="top" class="webhook-config-form">
        <NFormItem label="Compose 服务名" required
          ><NInput v-model:value="pipelineFields.serviceName" placeholder="app"
        /></NFormItem>
        <NFormItem label="默认部署环境"
          ><NSelect
            v-model:value="pipelineFields.defaultEnvironmentId"
            clearable
            :loading="environmentsLoading"
            :disabled="!environments.length"
            placeholder="仅构建，不自动部署"
            :options="
              environments.map((item) => ({
                label: `${item.name} · ${servers.find((server) => server.id === item.server_id)?.name || item.server_id}`,
                value: item.id,
              }))
            "
        /></NFormItem>
        <NButton type="primary" secondary :loading="pipelineSaving" @click="savePipeline"
          >保存自动部署配置</NButton
        >
      </NForm>
    </section>
    <div class="environment-toolbar">
      <p>一个环境绑定一台服务器，同一环境部署并发固定为 1。</p>
      <NButton type="primary" :disabled="!servers.length" @click="openCreate"
        ><template #icon><NIcon :component="Plus" /></template>新建环境</NButton
      >
    </div>
    <NDataTable
      v-if="environments.length || environmentsLoading"
      :data="environments"
      :columns="columns"
      :loading="environmentsLoading"
      :bordered="false"
      :single-line="false"
      :scroll-x="850"
    />
    <EmptyState
      v-else
      :icon="ServerCog"
      title="没有部署环境"
      description="绑定服务器、Compose 与健康检查后，流水线才能进入部署阶段。"
      action-label="新建环境"
      @action="openCreate"
    />
  </NModal>

  <NModal
    v-model:show="showEditor"
    preset="card"
    :title="editingId ? '编辑部署环境' : '新建部署环境'"
    class="environment-editor"
    :bordered="false"
  >
    <NForm label-placement="top">
      <div class="form-grid two">
        <NFormItem label="环境名称" required
          ><NInput v-model:value="form.name" placeholder="production"
        /></NFormItem>
        <NFormItem label="目标服务器" required
          ><NSelect
            v-model:value="form.server_id"
            placeholder="选择服务器"
            :options="
              servers.map((item) => ({
                label: `${item.name} · ${item.host}${item.enabled ? '' : ' · 已停用'}`,
                value: item.id,
              }))
            "
        /></NFormItem>
      </div>
      <div class="form-grid two">
        <NFormItem label="Compose 来源"
          ><NSelect
            v-model:value="form.compose_source"
            :options="[
              { label: '仓库文件', value: 'repository' },
              { label: '平台内联', value: 'inline' },
            ]"
        /></NFormItem>
        <NFormItem label="Compose 路径"
          ><NInput v-model:value="form.compose_path" placeholder="compose.yaml"
        /></NFormItem>
      </div>
      <NFormItem v-if="form.compose_source === 'inline'" label="Compose 内容" required
        ><NInput
          v-model:value="form.compose_content"
          type="textarea"
          :rows="10"
          class="code-input"
          placeholder="services: ..."
      /></NFormItem>
      <NFormItem label="远端部署目录" required
        ><NInput
          v-model:value="form.deploy_path"
          class="mono"
          placeholder="/opt/apps/billing-service"
      /></NFormItem>
      <div class="form-grid two">
        <NFormItem label="环境变量（每行 KEY=value）"
          ><NInput
            v-model:value="form.envConfig"
            type="textarea"
            :rows="8"
            class="code-input"
            placeholder="SPRING_PROFILES_ACTIVE=prod"
        /></NFormItem>
        <NFormItem label="健康检查 JSON"
          ><div class="field-stack">
            <NInput
              v-model:value="form.healthcheck"
              type="textarea"
              :rows="8"
              class="code-input"
              placeholder='{"kind":"http","url":"http://127.0.0.1:8080/health"}'
            /><small
              >HTTP/TCP 检查通过 SSH 在目标服务器发起，<code>127.0.0.1</code>
              表示目标服务器本机。</small
            >
          </div></NFormItem
        >
      </div>
    </NForm>
    <template #footer
      ><div class="modal-footer">
        <NButton @click="showEditor = false">取消</NButton
        ><NButton type="primary" :loading="environmentSaving" @click="saveEnvironment"
          ><template #icon><NIcon :component="ServerCog" /></template>保存环境</NButton
        >
      </div></template
    >
  </NModal>
</template>

<script setup lang="ts">
import { h, reactive, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NDataTable,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NModal,
  NSelect,
  NTag,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import { GitBranch, Layers3, Pencil, Plus, ServerCog, Trash2 } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Environment, EnvironmentInput, Project, Server } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import {
  formatJson,
  formatKeyValue,
  isSafeRepositoryPath,
  mergePipelineConfig,
  parseJsonObject,
  parseKeyValue,
  readPipelineFields,
  validServiceName,
  validateHealthcheck,
} from '@/utils/projectConfig'

const props = defineProps<{
  show: boolean
  project: Project | null
  servers: Server[]
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  'project-updated': [project: Project]
}>()

const message = useMessage()
const dialog = useDialog()
const environments = ref<Environment[]>([])
const environmentsLoading = ref(false)
const environmentSaving = ref(false)
const pipelineSaving = ref(false)
const showEditor = ref(false)
const editingId = ref<string | null>(null)
const pipelineFields = reactive({ serviceName: 'app', defaultEnvironmentId: null as string | null })

const emptyEnvironmentForm = () => ({
  name: '',
  server_id: '',
  compose_source: 'repository' as 'repository' | 'inline',
  compose_path: 'compose.yaml',
  compose_content: '',
  deploy_path: '/opt/apps/',
  envConfig: '',
  healthcheck: '{\n  "kind": "compose",\n  "timeout_seconds": 120\n}',
})

const form = reactive(emptyEnvironmentForm())

async function loadEnvironments(): Promise<void> {
  /** 读取项目环境列表并同步当前选中环境。 */
  const projectId = props.project?.id
  if (!projectId) return
  environmentsLoading.value = true
  try {
    const items = await api.projects.environments(projectId)
    if (props.show && props.project?.id === projectId) environments.value = items
  } catch (error) {
    environments.value = []
    message.error(error instanceof Error ? error.message : '环境列表加载失败')
  } finally {
    environmentsLoading.value = false
  }
}

function openCreate(): void {
  /** 打开新环境表单并选择第一个启用服务器作为默认目标。 */
  if (!props.servers.length) {
    message.warning('请先登记服务器')
    return
  }
  editingId.value = null
  const defaultServer = props.servers.find((item) => item.enabled) || props.servers[0]
  const projectSlug = props.project?.name.toLowerCase().replace(/[^a-z0-9_-]+/g, '-') || 'app'
  Object.assign(form, emptyEnvironmentForm(), {
    server_id: defaultServer?.id || '',
    deploy_path: `/opt/apps/${projectSlug}`,
  })
  showEditor.value = true
}

function openEdit(environment: Environment): void {
  /** 将现有环境复制到表单，避免直接修改列表对象。 */
  editingId.value = environment.id
  Object.assign(form, {
    name: environment.name,
    server_id: environment.server_id,
    compose_source: environment.compose_source,
    compose_path: environment.compose_path,
    compose_content: environment.compose_content || '',
    deploy_path: environment.deploy_path,
    envConfig: formatKeyValue(environment.env_config),
    healthcheck: formatJson(environment.healthcheck),
  })
  showEditor.value = true
}

async function saveEnvironment(): Promise<void> {
  /** 校验并保存环境部署配置及其健康检查参数。 */
  const project = props.project
  if (!project) return
  if (!form.name.trim() || !form.server_id) {
    message.warning('请填写环境名称并选择服务器')
    return
  }
  if (!form.deploy_path.startsWith('/')) {
    message.warning('部署目录必须是 Linux 绝对路径')
    return
  }
  if (form.compose_source === 'repository' && !isSafeRepositoryPath(form.compose_path)) {
    message.warning('Compose 路径必须是仓库内相对路径')
    return
  }
  if (form.compose_source === 'inline' && !form.compose_content.trim()) {
    message.warning('内联 Compose 内容不能为空')
    return
  }

  let payload: EnvironmentInput
  try {
    const healthcheck = parseJsonObject(form.healthcheck, '健康检查')
    validateHealthcheck(healthcheck)
    payload = {
      name: form.name.trim(),
      server_id: form.server_id,
      compose_source: form.compose_source,
      compose_path: form.compose_path.trim() || 'compose.yaml',
      compose_content: form.compose_source === 'inline' ? form.compose_content : null,
      deploy_path: form.deploy_path.trim(),
      env_config: parseKeyValue(form.envConfig),
      healthcheck,
    }
  } catch (error) {
    message.warning(error instanceof Error ? error.message : '环境配置无效')
    return
  }

  environmentSaving.value = true
  try {
    if (editingId.value) await api.projects.updateEnvironment(project.id, editingId.value, payload)
    else await api.projects.createEnvironment(project.id, payload)
    message.success(editingId.value ? '环境已更新' : '环境已创建')
    showEditor.value = false
    await loadEnvironments()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '环境保存失败')
  } finally {
    environmentSaving.value = false
  }
}

async function savePipeline(): Promise<void> {
  /** 保存项目级流水线配置，并保持高级字段不被表单覆盖。 */
  const project = props.project
  if (!project) return
  if (!validServiceName(pipelineFields.serviceName)) {
    message.warning('Compose 服务名必须以字母或数字开头，且只能包含字母、数字、点、下划线和短横线')
    return
  }
  if (
    pipelineFields.defaultEnvironmentId &&
    !environments.value.some((item) => item.id === pipelineFields.defaultEnvironmentId)
  ) {
    message.warning('默认部署环境不属于当前项目，请重新选择')
    return
  }

  pipelineSaving.value = true
  try {
    const updated = await api.projects.update(project.id, {
      pipeline_config: mergePipelineConfig(project.pipeline_config, pipelineFields),
    })
    emit('project-updated', updated)
    message.success('Webhook 自动部署配置已保存')
  } catch (error) {
    message.error(error instanceof Error ? error.message : 'Webhook 自动部署配置保存失败')
  } finally {
    pipelineSaving.value = false
  }
}

function removeEnvironment(environment: Environment): void {
  /** 在确认后删除环境，避免误删仍绑定部署目标的配置。 */
  const project = props.project
  if (!project) return
  const clearsDefault = pipelineFields.defaultEnvironmentId === environment.id
  dialog.warning({
    title: '删除部署环境',
    content: `确定删除“${environment.name}”？存在部署历史时服务端会拒绝删除。${clearsDefault ? '删除成功后会同时清除 Webhook 默认部署环境。' : ''}`,
    positiveText: '确认删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.projects.removeEnvironment(project.id, environment.id)
        message.success('环境已删除')
        await loadEnvironments()
      } catch (error) {
        message.error(error instanceof Error ? error.message : '环境删除失败')
        return
      }
      if (clearsDefault) {
        pipelineFields.defaultEnvironmentId = null
        try {
          const updated = await api.projects.update(project.id, {
            pipeline_config: mergePipelineConfig(project.pipeline_config, pipelineFields),
          })
          emit('project-updated', updated)
          message.success('Webhook 默认部署环境已清除')
        } catch (error) {
          message.error(
            error instanceof Error
              ? `环境已删除，但清除默认部署配置失败：${error.message}`
              : '环境已删除，但清除默认部署配置失败',
          )
        }
      }
    },
  })
}

const columns: DataTableColumns<Environment> = [
  {
    title: '环境',
    key: 'name',
    minWidth: 170,
    render: (row) =>
      h('div', { class: 'environment-cell' }, [
        h('div', { class: 'environment-title' }, [
          h('strong', row.name),
          pipelineFields.defaultEnvironmentId === row.id
            ? h(
                NTag,
                { size: 'tiny', type: 'success', bordered: false },
                { default: () => 'WEBHOOK 默认' },
              )
            : null,
        ]),
        h('small', props.servers.find((item) => item.id === row.server_id)?.name || row.server_id),
      ]),
  },
  {
    title: 'Compose',
    key: 'compose',
    minWidth: 190,
    render: (row) =>
      h('div', { class: 'environment-cell' }, [
        h('strong', row.compose_source === 'inline' ? '平台内联' : '仓库文件'),
        h('small', row.compose_path),
      ]),
  },
  {
    title: '部署目录',
    key: 'deploy_path',
    minWidth: 210,
    render: (row) => h('code', row.deploy_path),
  },
  {
    title: '健康检查',
    key: 'healthcheck',
    width: 120,
    render: (row) => String(row.healthcheck.kind || row.healthcheck.type || 'compose'),
  },
  {
    title: '',
    key: 'actions',
    width: 104,
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            'aria-label': `编辑环境 ${row.name}`,
            onClick: () => openEdit(row),
          },
          { icon: () => h(NIcon, { component: Pencil }) },
        ),
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            type: 'error',
            'aria-label': `删除环境 ${row.name}`,
            onClick: () => removeEnvironment(row),
          },
          { icon: () => h(NIcon, { component: Trash2 }) },
        ),
      ]),
  },
]

watch([() => props.show, () => props.project], ([show, project]) => {
  if (!show || !project) {
    showEditor.value = false
    return
  }
  Object.assign(pipelineFields, readPipelineFields(project.pipeline_config))
  void loadEnvironments()
})
</script>

<style scoped>
.environment-modal {
  width: min(980px, calc(100vw - 30px));
}
.environment-editor {
  width: min(880px, calc(100vw - 30px));
}
.modal-heading {
  display: flex;
  align-items: center;
  gap: 11px;
}
.modal-heading > span {
  display: grid;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.08);
  place-items: center;
}
.modal-heading h2 {
  margin: 0;
  font-size: 18px;
}
.environment-alert {
  margin-bottom: 14px;
}
.webhook-config {
  margin-bottom: 18px;
  padding: 14px;
  border: 1px solid rgba(94, 161, 255, 0.2);
  border-radius: 11px;
  background: rgba(94, 161, 255, 0.035);
}
.automation-heading {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 13px;
  color: #77adff;
}
.automation-heading > div {
  display: flex;
  flex-direction: column;
}
.automation-heading b {
  font-size: 11px;
}
.automation-heading small {
  margin-top: 3px;
  color: #6c7988;
  font-size: 9px;
}
.webhook-config :deep(.n-form-item) {
  margin-bottom: 0;
}
.webhook-config-form {
  display: grid;
  grid-template-columns: minmax(180px, 0.7fr) minmax(260px, 1fr) auto;
  align-items: end;
  gap: 14px;
}
.webhook-config-form > .n-button {
  margin-bottom: 1px;
}
.environment-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.environment-toolbar p {
  margin: 0;
  color: #718090;
  font-size: 10px;
}
.form-grid {
  display: grid;
  gap: 14px;
}
.form-grid.two {
  grid-template-columns: 1fr 1fr;
}
.field-stack {
  display: grid;
  width: 100%;
  gap: 6px;
}
.field-stack small {
  color: #657281;
  font-size: 9px;
  line-height: 1.45;
}
:deep(.environment-cell) {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
:deep(.environment-title) {
  display: flex;
  align-items: center;
  gap: 7px;
}
:deep(.environment-cell strong) {
  font-size: 12px;
}
:deep(.environment-cell small) {
  overflow: hidden;
  max-width: 220px;
  margin-top: 3px;
  color: #667382;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.row-actions) {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
}
:deep(.code-input textarea) {
  font-family: 'JetBrains Mono', Consolas, monospace !important;
  font-size: 10px !important;
  line-height: 1.6 !important;
  tab-size: 2;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
@media (max-width: 860px) {
  .webhook-config-form {
    grid-template-columns: 1fr 1fr;
  }
  .webhook-config-form > .n-button {
    grid-column: 1/-1;
  }
}
@media (max-width: 760px) {
  .form-grid.two,
  .webhook-config-form {
    grid-template-columns: 1fr;
  }
  .webhook-config-form > .n-button {
    grid-column: auto;
  }
  .environment-toolbar {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
