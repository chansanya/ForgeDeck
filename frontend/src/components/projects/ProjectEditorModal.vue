<!-- 项目编辑弹窗：维护仓库、Dockerfile、Registry 和流水线基础配置。 -->
<template>
  <NModal
    :show="show"
    preset="card"
    :title="editing ? '编辑项目' : '新建项目'"
    class="editor-modal"
    :bordered="false"
    @update:show="emit('update:show', $event)"
  >
    <NForm ref="formRef" :model="form" :rules="rules" label-placement="top">
      <div class="form-grid two">
        <NFormItem label="项目名称" path="name"
          ><NInput v-model:value="form.name" placeholder="billing-service"
        /></NFormItem>
        <NFormItem label="Dockerfile 来源" path="dockerfile_source"
          ><NSelect
            v-model:value="form.dockerfile_source"
            :options="[
              { label: '仓库文件', value: 'repository' },
              { label: '平台内联', value: 'inline' },
            ]"
        /></NFormItem>
      </div>
      <NFormItem label="仓库地址" path="repo_url"
        ><NInput
          v-model:value="form.repo_url"
          placeholder="https://gitlab.example.com/team/project.git"
          ><template #prefix><NIcon :component="GitFork" /></template></NInput
      ></NFormItem>
      <div class="template-row">
        <div>
          <Sparkles :size="17" /><span
            ><b>项目模板</b
            ><small>选择后会复制 Dockerfile 到平台内联内容，复制完仍可自由修改。</small></span
          >
        </div>
        <NSelect
          :value="selectedTemplateId"
          clearable
          placeholder="选择 Java / Node.js / Python 模板"
          :options="
            templates.map((item) => ({ label: `${item.name} · ${item.language}`, value: item.id }))
          "
          @update:value="applyTemplate"
        />
      </div>
      <div class="form-grid two">
        <NFormItem label="默认分支" path="default_branch"
          ><NInput v-model:value="form.default_branch" placeholder="main"
            ><template #prefix><NIcon :component="GitBranch" /></template></NInput
        ></NFormItem>
        <NFormItem label="Registry 镜像" path="image_repository" :required="form.enabled"
          ><div class="field-stack">
            <NInput
              v-model:value="form.image_repository"
              placeholder="registry.example.com/team/app"
            /><small>启用项目会通过 Buildx 固定推送镜像；停用草稿可暂不填写。</small>
          </div></NFormItem
        >
      </div>
      <div class="form-grid three">
        <NFormItem label="Git HTTPS 凭据"
          ><div class="field-stack">
            <NSelect
              v-model:value="form.git_credential_id"
              clearable
              placeholder="公开仓库无需选择"
              :options="credentialOptions(gitCredentials)"
            /><small>v1 私有仓库使用 HTTPS 用户名与 Access Token，暂不支持 Git SSH 私钥。</small>
          </div></NFormItem
        >
        <NFormItem label="Webhook 密钥"
          ><NSelect
            v-model:value="form.webhook_credential_id"
            clearable
            placeholder="选择验签密钥"
            :options="credentialOptions(webhookCredentials)"
        /></NFormItem>
        <NFormItem label="Registry 凭据"
          ><NSelect
            v-model:value="form.registry_credential_id"
            clearable
            placeholder="选择推送凭据"
            :options="credentialOptions(registryCredentials)"
        /></NFormItem>
      </div>
      <div class="form-grid two">
        <NFormItem label="构建上下文" path="build_context"
          ><NInput v-model:value="form.build_context" placeholder="."
        /></NFormItem>
        <NFormItem label="Dockerfile 路径" path="dockerfile_path"
          ><NInput v-model:value="form.dockerfile_path" placeholder="Dockerfile"
        /></NFormItem>
      </div>
      <NFormItem
        v-if="form.dockerfile_source === 'inline'"
        label="Dockerfile 内容"
        path="dockerfile_content"
        ><NInput
          v-model:value="form.dockerfile_content"
          type="textarea"
          :rows="11"
          class="code-input"
          placeholder="FROM ..."
      /></NFormItem>
      <section class="automation-card">
        <div class="automation-heading">
          <GitBranch :size="17" />
          <div>
            <b>Webhook 自动部署</b
            ><small>默认环境为空时 Webhook 只构建镜像，不会进入部署阶段。</small>
          </div>
        </div>
        <div class="form-grid two">
          <NFormItem label="Compose 服务名" required
            ><NInput v-model:value="pipelineFields.serviceName" placeholder="app"
          /></NFormItem>
          <NFormItem label="默认部署环境"
            ><div class="field-stack">
              <NSelect
                v-model:value="pipelineFields.defaultEnvironmentId"
                clearable
                :loading="environmentsLoading"
                :disabled="!editing"
                :placeholder="editing ? '仅构建，不自动部署' : '创建项目后再选择环境'"
                :options="environments.map((item) => ({ label: item.name, value: item.id }))"
              /><small
                >对应
                <code>pipeline_config.default_environment_id</code>，只允许选择本项目环境。</small
              >
            </div></NFormItem
          >
        </div>
      </section>
      <div class="form-grid two">
        <NFormItem label="构建参数 JSON"
          ><NInput
            v-model:value="projectText.buildArgs"
            type="textarea"
            :rows="6"
            class="code-input"
            placeholder='{"APP_ENV":"production"}'
        /></NFormItem>
        <NFormItem label="高级流水线配置 JSON"
          ><div class="field-stack">
            <NInput
              v-model:value="projectText.pipelineConfig"
              type="textarea"
              :rows="6"
              class="code-input"
              placeholder='{"min_free_bytes":1073741824}'
            /><small
              ><code>service_name</code> 与
              <code>default_environment_id</code> 请使用上方专用字段，其余键会原样保留。</small
            >
          </div></NFormItem
        >
      </div>
      <div class="switch-row">
        <div><strong>允许触发流水线</strong><span>停用后 Webhook 和手动运行都会被拒绝</span></div>
        <NSwitch v-model:value="form.enabled" />
      </div>
    </NForm>
    <template #footer
      ><div class="modal-footer">
        <NButton @click="emit('update:show', false)">取消</NButton
        ><NButton type="primary" :loading="saving" @click="submit"
          ><template #icon><NIcon :component="Boxes" /></template>保存项目</NButton
        >
      </div></template
    >
  </NModal>
</template>

<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import {
  NButton,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NModal,
  NSelect,
  NSwitch,
  useMessage,
  type FormInst,
  type FormRules,
} from 'naive-ui'
import { Boxes, GitBranch, GitFork, Sparkles } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Credential, Environment, Project, ProjectInput, ProjectTemplate } from '@/api/types'
import {
  advancedPipelineConfig,
  formatJson,
  isSafeRepositoryPath,
  mergePipelineConfig,
  parseJsonObject,
  parseStringJsonObject,
  readPipelineFields,
  validServiceName,
} from '@/utils/projectConfig'

const props = defineProps<{
  show: boolean
  project: Project | null
  credentials: Credential[]
  templates: ProjectTemplate[]
  saving: boolean
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  save: [payload: ProjectInput]
}>()

const message = useMessage()
const formRef = ref<FormInst | null>(null)
const environments = ref<Environment[]>([])
const environmentsLoading = ref(false)
const environmentsLoaded = ref(false)
const selectedTemplateId = ref<string | null>(null)
const projectText = reactive({ buildArgs: '{}', pipelineConfig: '{}' })
const pipelineFields = reactive({ serviceName: 'app', defaultEnvironmentId: null as string | null })

const emptyForm = (): ProjectInput => ({
  name: '',
  repo_url: '',
  default_branch: 'main',
  git_credential_id: null,
  webhook_credential_id: null,
  registry_credential_id: null,
  dockerfile_source: 'repository',
  dockerfile_path: 'Dockerfile',
  dockerfile_content: null,
  build_context: '.',
  image_repository: '',
  build_args: {},
  pipeline_config: {},
  enabled: true,
})

const form = reactive<ProjectInput>(emptyForm())
const editing = computed(() => Boolean(props.project))
const gitCredentials = computed(() => props.credentials.filter((item) => item.kind === 'git'))
const webhookCredentials = computed(() =>
  props.credentials.filter((item) => item.kind === 'webhook'),
)
const registryCredentials = computed(() =>
  props.credentials.filter((item) => item.kind === 'registry'),
)

const rules: FormRules = {
  name: { required: true, message: '请输入项目名称', trigger: 'blur' },
  repo_url: { required: true, message: '请输入 Git 仓库地址', trigger: 'blur' },
  default_branch: { required: true, message: '请输入默认分支', trigger: 'blur' },
  image_repository: {
    trigger: ['input', 'blur'],
    validator: () =>
      !form.enabled ||
      Boolean(form.image_repository?.trim()) ||
      new Error('启用项目必须配置 Registry 镜像地址'),
  },
  dockerfile_path: { required: true, message: '请输入 Dockerfile 路径', trigger: 'blur' },
}

function credentialOptions(items: Credential[]) {
  /** 将凭据实体转换为下拉选项，页面只展示安全元数据。 */
  return items.map((item) => ({ label: item.name, value: item.id }))
}

async function loadEnvironments(projectId: string): Promise<void> {
  /** 加载项目环境供编辑器选择部署目标。 */
  environmentsLoading.value = true
  environmentsLoaded.value = false
  try {
    const items = await api.projects.environments(projectId)
    if (props.show && props.project?.id === projectId) environments.value = items
    environmentsLoaded.value = true
  } catch (error) {
    environments.value = []
    message.error(error instanceof Error ? error.message : '部署环境加载失败')
  } finally {
    environmentsLoading.value = false
  }
}

function hydrate(): void {
  /** 用当前项目配置填充表单，未编辑项目时恢复默认值。 */
  const project = props.project
  selectedTemplateId.value = null
  environments.value = []
  environmentsLoaded.value = !project
  if (!project) {
    Object.assign(form, emptyForm())
    Object.assign(projectText, { buildArgs: '{}', pipelineConfig: '{}' })
    Object.assign(pipelineFields, { serviceName: 'app', defaultEnvironmentId: null })
  } else {
    Object.assign(form, {
      name: project.name,
      repo_url: project.repo_url,
      default_branch: project.default_branch,
      git_credential_id: project.git_credential_id,
      webhook_credential_id: project.webhook_credential_id,
      registry_credential_id: project.registry_credential_id,
      dockerfile_source: project.dockerfile_source,
      dockerfile_path: project.dockerfile_path,
      dockerfile_content: project.dockerfile_content,
      build_context: project.build_context,
      image_repository: project.image_repository,
      build_args: project.build_args,
      pipeline_config: project.pipeline_config,
      enabled: project.enabled,
    })
    Object.assign(projectText, {
      buildArgs: formatJson(project.build_args),
      pipelineConfig: formatJson(advancedPipelineConfig(project.pipeline_config)),
    })
    Object.assign(pipelineFields, readPipelineFields(project.pipeline_config))
    void loadEnvironments(project.id)
  }
  void nextTick(() => formRef.value?.restoreValidation())
}

function applyTemplate(templateId: string | null): void {
  /** 将白名单模板内容应用到表单，保留用户可继续编辑的配置。 */
  selectedTemplateId.value = templateId
  const template = props.templates.find((item) => item.id === templateId)
  if (!template) return
  form.dockerfile_source = 'inline'
  form.dockerfile_path = 'Dockerfile'
  form.dockerfile_content = template.dockerfile
  message.success(`已应用 ${template.name} 模板，可继续编辑 Dockerfile`)
}

async function submit(): Promise<void> {
  /** 校验并提交项目配置，失败时保留表单内容供用户修正。 */
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  if (form.dockerfile_source === 'inline' && !form.dockerfile_content?.trim()) {
    message.warning('内联 Dockerfile 不能为空')
    return
  }
  if (!isSafeRepositoryPath(form.dockerfile_path) || !isSafeRepositoryPath(form.build_context)) {
    message.warning('Dockerfile 与构建上下文必须是仓库内相对路径')
    return
  }
  if (form.enabled && !form.image_repository?.trim()) {
    message.warning('启用项目必须配置 Registry 镜像地址，Buildx 构建会固定推送镜像')
    return
  }
  if (!validServiceName(pipelineFields.serviceName)) {
    message.warning('Compose 服务名必须以字母或数字开头，且只能包含字母、数字、点、下划线和短横线')
    return
  }
  if (
    pipelineFields.defaultEnvironmentId &&
    environmentsLoaded.value &&
    !environments.value.some((item) => item.id === pipelineFields.defaultEnvironmentId)
  ) {
    message.warning('默认部署环境不属于当前项目，请重新选择')
    return
  }

  try {
    emit('save', {
      ...form,
      name: form.name.trim(),
      repo_url: form.repo_url.trim(),
      default_branch: form.default_branch.trim(),
      dockerfile_path: form.dockerfile_path.trim(),
      build_context: form.build_context.trim(),
      image_repository: form.image_repository?.trim() || null,
      build_args: parseStringJsonObject(projectText.buildArgs, '构建参数'),
      pipeline_config: mergePipelineConfig(
        parseJsonObject(projectText.pipelineConfig, '高级流水线配置'),
        pipelineFields,
      ),
    })
  } catch (error) {
    message.warning(error instanceof Error ? error.message : '项目 JSON 配置无效')
  }
}

watch(
  () => props.show,
  (show) => {
    if (show) hydrate()
  },
)
</script>

<style scoped>
.editor-modal {
  width: min(900px, calc(100vw - 30px));
}
.form-grid {
  display: grid;
  gap: 14px;
}
.form-grid.two {
  grid-template-columns: 1fr 1fr;
}
.form-grid.three {
  grid-template-columns: repeat(3, 1fr);
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
.field-stack code {
  color: #8fa34f;
}
.template-row {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(280px, 1fr);
  align-items: center;
  gap: 18px;
  margin-bottom: 18px;
  padding: 13px 14px;
  border: 1px solid rgba(199, 255, 74, 0.18);
  border-radius: 11px;
  background: rgba(199, 255, 74, 0.04);
}
.template-row > div {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #c7ff4a;
}
.template-row span {
  display: flex;
  flex-direction: column;
}
.template-row b {
  font-size: 11px;
}
.template-row small {
  margin-top: 3px;
  color: #718091;
  font-size: 9px;
}
.automation-card {
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
.automation-card .form-grid :deep(.n-form-item) {
  margin-bottom: 0;
}
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 13px 14px;
  border: 1px solid #25303b;
  border-radius: 10px;
  background: #0c1219;
}
.switch-row div {
  display: flex;
  flex-direction: column;
}
.switch-row strong {
  font-size: 12px;
}
.switch-row span {
  margin-top: 3px;
  color: #687584;
  font-size: 10px;
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
@media (max-width: 760px) {
  .form-grid.two,
  .form-grid.three,
  .template-row {
    grid-template-columns: 1fr;
  }
}
</style>
