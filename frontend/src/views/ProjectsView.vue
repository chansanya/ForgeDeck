<!-- 项目页面：管理源码、构建配置及项目所属部署环境。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="SOURCE & BUILD"
      title="项目"
      description="定义源码入口、构建上下文与容器产物。流水线只运行已固化的配置快照。"
    >
      <template #actions>
        <NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        >
        <NButton type="primary" @click="openCreate"
          ><template #icon><NIcon :component="Plus" /></template>新建项目</NButton
        >
      </template>
    </PageHeader>

    <section class="panel table-panel">
      <div class="table-meta">
        <span
          ><b>{{ projects.length }}</b> 个项目</span
        ><span class="mono">BUILD CONCURRENCY / 1</span>
      </div>
      <NDataTable
        v-if="projects.length || loading"
        :columns="columns"
        :data="projects"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :scroll-x="1240"
      />
      <EmptyState
        v-else
        :icon="GitFork"
        title="还没有项目"
        description="登记第一个 Git 仓库和 Dockerfile，交付链路才算真正有了起点。"
        action-label="新建项目"
        @action="openCreate"
      />
    </section>

    <ProjectEditorModal
      v-model:show="showEditor"
      :project="editingProject"
      :credentials="credentials"
      :templates="templates"
      :saving="saving"
      @save="saveProject"
    />

    <NModal
      v-model:show="showRun"
      preset="card"
      :title="`触发流水线 · ${runProject?.name || ''}`"
      class="run-modal"
      :bordered="false"
    >
      <NForm label-placement="top">
        <NFormItem label="Commit SHA" required
          ><NInput
            v-model:value="runForm.commit_sha"
            class="mono"
            placeholder="输入完整的 40 或 64 位 Git commit SHA"
        /></NFormItem>
        <NFormItem label="Git Ref" required
          ><NInput v-model:value="runForm.ref" class="mono" placeholder="main 或 refs/heads/main"
        /></NFormItem>
        <NFormItem label="部署环境（可选）"
          ><NSelect
            v-model:value="runForm.environment_id"
            clearable
            placeholder="仅构建，不部署"
            :options="runEnvironments.map((item) => ({ label: item.name, value: item.id }))"
        /></NFormItem>
      </NForm>
      <template #footer
        ><div class="modal-footer">
          <NButton @click="showRun = false">取消</NButton
          ><NButton type="primary" :loading="running" @click="executeRun"
            ><template #icon><NIcon :component="Play" /></template>创建运行</NButton
          >
        </div></template
      >
    </NModal>

    <EnvironmentManagerModal
      v-model:show="showEnvironmentManager"
      :project="environmentProject"
      :servers="servers"
      @project-updated="updateProject"
    />
  </div>
</template>

<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
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
import {
  GitBranch,
  GitFork,
  MoreHorizontal,
  Play,
  Plus,
  RefreshCw,
  ServerCog,
  Trash2,
} from 'lucide-vue-next'
import { api } from '@/api/client'
import type {
  Credential,
  Environment,
  Project,
  ProjectInput,
  ProjectTemplate,
  Server,
} from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import EnvironmentManagerModal from '@/components/projects/EnvironmentManagerModal.vue'
import ProjectEditorModal from '@/components/projects/ProjectEditorModal.vue'
import PageHeader from '@/components/PageHeader.vue'
import { formatDate } from '@/utils/format'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const saving = ref(false)
const running = ref(false)
const projects = ref<Project[]>([])
const credentials = ref<Credential[]>([])
const templates = ref<ProjectTemplate[]>([])
const servers = ref<Server[]>([])
const showEditor = ref(false)
const showRun = ref(false)
const showEnvironmentManager = ref(false)
const editingProject = ref<Project | null>(null)
const runProject = ref<Project | null>(null)
const environmentProject = ref<Project | null>(null)
const runEnvironments = ref<Environment[]>([])
const runForm = reactive({ commit_sha: '', ref: 'main', environment_id: null as string | null })

async function load(): Promise<void> {
  /** 加载项目、服务器、凭据和模板，为编辑器准备关联数据。 */
  loading.value = true
  try {
    const [projectList, credentialList, templateList, serverList] = await Promise.all([
      api.projects.list(),
      api.credentials.list(),
      api.templates.list(),
      api.servers.list(),
    ])
    projects.value = projectList
    credentials.value = credentialList
    templates.value = templateList
    servers.value = serverList
    if (environmentProject.value) {
      environmentProject.value =
        projectList.find((item) => item.id === environmentProject.value?.id) || null
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : '项目配置加载失败')
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  /** 打开新项目编辑器。 */
  editingProject.value = null
  showEditor.value = true
}

function openEdit(project: Project): void {
  /** 打开现有项目编辑器并保留其配置快照边界。 */
  editingProject.value = project
  showEditor.value = true
}

async function saveProject(payload: ProjectInput): Promise<void> {
  /** 创建或更新项目，并刷新列表中的当前实体。 */
  saving.value = true
  try {
    if (editingProject.value) await api.projects.update(editingProject.value.id, payload)
    else await api.projects.create(payload)
    message.success(editingProject.value ? '项目已更新' : '项目已创建')
    showEditor.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '项目保存失败')
  } finally {
    saving.value = false
  }
}

async function openRun(project: Project): Promise<void> {
  /** 打开项目运行参数对话框。 */
  runProject.value = project
  Object.assign(runForm, { commit_sha: '', ref: project.default_branch, environment_id: null })
  showRun.value = true
  try {
    runEnvironments.value = await api.projects.environments(project.id)
  } catch (error) {
    runEnvironments.value = []
    message.error(error instanceof Error ? error.message : '部署环境加载失败')
  }
}

async function executeRun(): Promise<void> {
  /** 提交流水线触发请求，commit 和配置快照由后端固化。 */
  if (!runProject.value) return
  if (!/^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/.test(runForm.commit_sha.trim())) {
    message.warning('Commit SHA 必须是完整的 40 或 64 位十六进制对象 ID')
    return
  }
  if (!runForm.ref.trim()) {
    message.warning('请输入 Git ref')
    return
  }
  running.value = true
  try {
    const result = await api.projects.run(runProject.value.id, {
      commit_sha: runForm.commit_sha.trim(),
      ref: runForm.ref.trim(),
      environment_id: runForm.environment_id || undefined,
    })
    message.success('流水线已进入队列')
    showRun.value = false
    await router.push({ name: 'run-detail', params: { id: result.id } })
  } catch (error) {
    message.error(error instanceof Error ? error.message : '流水线触发失败')
  } finally {
    running.value = false
  }
}

function openEnvironments(project: Project): void {
  /** 打开项目环境管理器。 */
  environmentProject.value = project
  showEnvironmentManager.value = true
}

function updateProject(updated: Project): void {
  /** 用保存后的项目实体替换列表项，避免页面状态与服务端脱节。 */
  const index = projects.value.findIndex((item) => item.id === updated.id)
  if (index >= 0) projects.value[index] = updated
  if (environmentProject.value?.id === updated.id) environmentProject.value = updated
  if (editingProject.value?.id === updated.id) editingProject.value = updated
}

function remove(project: Project): void {
  /** 在确认后删除项目，后端负责检查运行和环境引用。 */
  dialog.warning({
    title: '删除项目',
    content: `确定删除“${project.name}”？历史运行和部署记录不会自动删除。`,
    positiveText: '确认删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.projects.remove(project.id)
        message.success('项目已删除')
        await load()
      } catch (error) {
        message.error(error instanceof Error ? error.message : '项目删除失败')
      }
    },
  })
}

const columns: DataTableColumns<Project> = [
  {
    title: '项目',
    key: 'name',
    minWidth: 200,
    render: (row) =>
      h('div', { class: 'project-cell' }, [
        h('span', { class: 'project-symbol' }, row.name.slice(0, 2).toUpperCase()),
        h('div', [h('strong', row.name), h('small', row.image_repository || '尚未配置镜像仓库')]),
      ]),
  },
  {
    title: '仓库',
    key: 'repo_url',
    minWidth: 260,
    ellipsis: { tooltip: true },
    render: (row) => h('span', { class: 'mono repo-url' }, row.repo_url),
  },
  {
    title: '默认分支',
    key: 'branch',
    width: 130,
    render: (row) =>
      h('span', { class: 'branch-label' }, [h(GitBranch, { size: 13 }), row.default_branch]),
  },
  {
    title: '构建文件',
    key: 'dockerfile',
    minWidth: 150,
    render: (row) =>
      h(
        'span',
        { class: 'mono muted' },
        row.dockerfile_source === 'inline' ? 'INLINE' : row.dockerfile_path,
      ),
  },
  {
    title: '状态',
    key: 'enabled',
    width: 90,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: row.enabled ? 'success' : 'default', bordered: false },
        { default: () => (row.enabled ? '启用' : '停用') },
      ),
  },
  { title: '更新', key: 'updated_at', width: 130, render: (row) => formatDate(row.updated_at) },
  {
    title: '',
    key: 'actions',
    width: 205,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          {
            size: 'small',
            type: 'primary',
            secondary: true,
            disabled: !row.enabled || !row.image_repository,
            onClick: () => openRun(row),
          },
          { icon: () => h(NIcon, { component: Play }), default: () => '运行' },
        ),
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            title: '部署环境',
            'aria-label': `管理项目 ${row.name} 的部署环境`,
            onClick: () => openEnvironments(row),
          },
          { icon: () => h(NIcon, { component: ServerCog }) },
        ),
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            title: '编辑项目',
            'aria-label': `编辑项目 ${row.name}`,
            onClick: () => openEdit(row),
          },
          { icon: () => h(NIcon, { component: MoreHorizontal }) },
        ),
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            type: 'error',
            title: '删除项目',
            'aria-label': `删除项目 ${row.name}`,
            onClick: () => remove(row),
          },
          { icon: () => h(NIcon, { component: Trash2 }) },
        ),
      ]),
  },
]

onMounted(load)
</script>

<style scoped>
.table-panel {
  padding: 8px 0 0;
}
.table-meta {
  display: flex;
  justify-content: space-between;
  padding: 13px 18px 17px;
  color: #6d7988;
  font-size: 11px;
}
.table-meta b {
  color: #c7ff4a;
  font-family: 'JetBrains Mono', monospace;
  font-size: 18px;
}
.table-meta .mono {
  font-size: 9px;
  letter-spacing: 0.08em;
}
:deep(.project-cell) {
  display: flex;
  align-items: center;
  gap: 11px;
}
:deep(.project-symbol) {
  display: grid;
  width: 35px;
  height: 35px;
  flex: 0 0 auto;
  border: 1px solid #293440;
  border-radius: 9px;
  color: #c7ff4a;
  background: #121923;
  place-items: center;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  font-weight: 700;
}
:deep(.project-cell div) {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
:deep(.project-cell strong) {
  font-size: 12px;
}
:deep(.project-cell small) {
  overflow: hidden;
  max-width: 220px;
  margin-top: 3px;
  color: #667382;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.repo-url) {
  color: #83909f;
  font-size: 10px;
}
:deep(.branch-label) {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #9aa6b4;
  font-size: 11px;
}
:deep(.row-actions) {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
}
.run-modal {
  width: min(520px, calc(100vw - 30px));
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
</style>
