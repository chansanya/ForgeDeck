<!-- 凭据页面：管理 Git、SSH、Registry 与 Webhook 等加密凭据元数据。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="SECRETS VAULT"
      title="凭据"
      description="Git、SSH、Registry 和 Webhook 密钥加密存储。接口只返回元数据，想从页面把明文捞出来这条路已经焊死。"
    >
      <template #actions
        ><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ><NButton type="primary" @click="create"
          ><template #icon><NIcon :component="Plus" /></template>添加凭据</NButton
        ></template
      >
    </PageHeader>
    <section class="security-strip panel">
      <ShieldCheck :size="18" />
      <div>
        <b>密钥与数据库分离备份</b
        ><span>前端不会持久化或回显任何凭据明文；提交成功后请妥善销毁本地临时副本。</span>
      </div>
    </section>
    <section class="panel table-panel">
      <NDataTable
        v-if="credentials.length || loading"
        :columns="columns"
        :data="credentials"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :scroll-x="1000"
      /><EmptyState
        v-else
        :icon="FileKey2"
        title="还没有凭据"
        description="添加访问 Git、SSH 或镜像仓库所需的密钥。明文只在本次提交中传输。"
        action-label="添加凭据"
        @action="create"
      />
    </section>
    <NModal
      v-model:show="showEditor"
      preset="card"
      :title="editingId ? '编辑凭据' : '添加凭据'"
      class="credential-modal"
      :bordered="false"
      @after-leave="resetForm"
      ><NForm ref="formRef" :model="form" :rules="rules" label-placement="top"
        ><div class="form-grid">
          <NFormItem label="名称" path="name"
            ><NInput v-model:value="form.name" placeholder="prod-deployer-key" /></NFormItem
          ><NFormItem label="类型" path="kind"
            ><div class="field-stack">
              <NSelect
                v-model:value="form.kind"
                :disabled="Boolean(editingId)"
                :options="Object.entries(kindLabels).map(([value, label]) => ({ value, label }))"
              /><small v-if="editingId">凭据类型不可变；需要换类型请新建凭据并重新绑定。</small>
            </div></NFormItem
          >
        </div>
        <div class="form-grid">
          <NFormItem label="用户名（可选）" path="username"
            ><NInput v-model:value="form.username" placeholder="deployer" /></NFormItem
          ><NFormItem label="服务地址（可选）" path="endpoint"
            ><NInput v-model:value="form.endpoint" placeholder="registry.example.com"
          /></NFormItem>
        </div>
        <NFormItem :label="editingId ? '新密钥 / 密码（留空不轮换）' : '密钥 / 密码'" path="secret"
          ><div class="field-stack">
            <NInput
              v-model:value="form.secret"
              :type="form.kind === 'ssh' ? 'textarea' : 'password'"
              :rows="form.kind === 'ssh' ? 10 : undefined"
              show-password-on="mousedown"
              class="secret-input"
              :placeholder="editingId ? '留空以保留当前加密密钥' : '仅本次提交可见'"
            /><small v-if="editingId">现有明文不会回显；填写新值后服务端才会将版本号加一。</small>
          </div></NFormItem
        ></NForm
      ><template #footer
        ><div class="modal-footer">
          <NButton @click="showEditor = false">取消</NButton
          ><NButton type="primary" :loading="saving" @click="save">{{
            editingId ? '保存修改' : '加密保存'
          }}</NButton>
        </div></template
      ></NModal
    >
  </div>
</template>

<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue'
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
import {
  FileKey2,
  Fingerprint,
  KeyRound,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Credential } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { formatDate } from '@/utils/format'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const saving = ref(false)
const credentials = ref<Credential[]>([])
const showEditor = ref(false)
const editingId = ref<string | null>(null)
const originalMetadata = ref<Record<string, unknown>>({})
const formRef = ref<FormInst | null>(null)
const form = reactive({ name: '', kind: 'ssh', username: '', endpoint: '', secret: '' })
const rules: FormRules = {
  name: { required: true, message: '请输入凭据名称' },
  kind: { required: true, message: '请选择凭据类型' },
  secret: { validator: () => Boolean(editingId.value || form.secret), message: '请输入密钥或密码' },
}
const kindLabels: Record<string, string> = {
  ssh: 'SSH 凭据',
  git: 'Git 凭据',
  registry: '镜像仓库',
  webhook: 'Webhook 密钥',
  smtp: 'SMTP 凭据',
  notification: '通知凭据',
}

async function load(): Promise<void> {
  /** 加载凭据安全元数据，接口不会返回 secret 明文。 */
  loading.value = true
  try {
    credentials.value = await api.credentials.list()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '凭据加载失败')
  } finally {
    loading.value = false
  }
}
function resetForm(): void {
  /** 清空凭据编辑表单并恢复默认类型。 */
  editingId.value = null
  originalMetadata.value = {}
  Object.assign(form, { name: '', kind: 'ssh', username: '', endpoint: '', secret: '' })
  formRef.value?.restoreValidation()
}
function create(): void {
  /** 打开新凭据表单。 */
  resetForm()
  showEditor.value = true
}
function edit(row: Credential): void {
  /** 编辑凭据元数据；已有 secret 必须由用户重新输入。 */
  editingId.value = row.id
  originalMetadata.value = { ...row.metadata }
  Object.assign(form, {
    name: row.name,
    kind: row.kind,
    username: metadataValue(row, 'username') === '—' ? '' : metadataValue(row, 'username'),
    endpoint: metadataValue(row, 'endpoint') === '—' ? '' : metadataValue(row, 'endpoint'),
    secret: '',
  })
  formRef.value?.restoreValidation()
  showEditor.value = true
}
function buildMetadata(): Record<string, unknown> {
  /** 将表单辅助字段转换为后端可校验的非敏感元数据。 */
  const metadata = { ...originalMetadata.value }
  if (form.username.trim()) metadata.username = form.username.trim()
  else delete metadata.username
  if (form.endpoint.trim()) metadata.endpoint = form.endpoint.trim()
  else delete metadata.endpoint
  return metadata
}
async function save(): Promise<void> {
  /** 创建或更新凭据，并在成功后清空敏感输入。 */
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      const rotateSecret = Boolean(form.secret)
      const updated = await api.credentials.update(editingId.value, {
        name: form.name.trim(),
        metadata: buildMetadata(),
        ...(rotateSecret ? { secret: form.secret } : {}),
      })
      message.success(
        rotateSecret
          ? `凭据已更新，密钥轮换至 v${updated.version}`
          : '凭据元数据已更新，密钥未轮换',
      )
    } else {
      await api.credentials.create({
        name: form.name.trim(),
        kind: form.kind,
        secret: form.secret,
        metadata: buildMetadata(),
      })
      message.success('凭据已加密保存')
    }
    form.secret = ''
    showEditor.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}
function metadataValue(row: Credential, key: string): string {
  /** 从安全元数据读取展示值，不触碰凭据密文。 */
  const value = row.metadata[key]
  return typeof value === 'string' && value ? value : '—'
}
function remove(row: Credential): void {
  /** 在确认后删除凭据，并由后端校验引用关系。 */
  dialog.warning({
    title: '删除凭据',
    content: `删除“${row.name}”可能导致相关仓库、服务器或 Registry 无法访问。确定继续？`,
    positiveText: '确认删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.credentials.remove(row.id)
        message.success('凭据已删除')
        await load()
      } catch (error) {
        message.error(error instanceof Error ? error.message : '删除失败')
      }
    },
  })
}

const columns: DataTableColumns<Credential> = [
  {
    title: '凭据',
    key: 'name',
    minWidth: 210,
    render: (row) =>
      h('div', { class: 'credential-cell' }, [
        h('span', [h(KeyRound, { size: 16 })]),
        h('div', [h('strong', row.name), h('small', metadataValue(row, 'endpoint'))]),
      ]),
  },
  { title: '类型', key: 'kind', width: 130, render: (row) => kindLabels[row.kind] || row.kind },
  {
    title: '用户',
    key: 'username',
    minWidth: 130,
    render: (row) => metadataValue(row, 'username'),
  },
  {
    title: '密钥状态',
    key: 'fingerprint',
    minWidth: 180,
    render: (row) =>
      h('span', { class: 'fingerprint mono' }, [
        h(Fingerprint, { size: 12 }),
        row.has_secret ? `已加密 · v${row.version}` : '未配置',
      ]),
  },
  { title: '创建时间', key: 'created_at', width: 140, render: (row) => formatDate(row.created_at) },
  {
    title: '',
    key: 'actions',
    width: 96,
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            'aria-label': `编辑凭据 ${row.name}`,
            onClick: () => edit(row),
          },
          { icon: () => h(NIcon, { component: Pencil }) },
        ),
        h(
          NButton,
          {
            quaternary: true,
            circle: true,
            type: 'error',
            'aria-label': `删除凭据 ${row.name}`,
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
.security-strip {
  display: flex;
  align-items: center;
  gap: 11px;
  margin-bottom: 15px;
  padding: 14px;
  color: #5ea1ff;
  background: linear-gradient(90deg, rgba(94, 161, 255, 0.06), transparent);
}
.security-strip div {
  display: flex;
  flex-direction: column;
}
.security-strip b {
  color: #aebdca;
  font-size: 11px;
}
.security-strip span {
  margin-top: 3px;
  color: #6e7b89;
  font-size: 10px;
}
.table-panel {
  padding-top: 4px;
}
:deep(.credential-cell) {
  display: flex;
  align-items: center;
  gap: 10px;
}
:deep(.credential-cell > span) {
  display: grid;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.07);
  place-items: center;
}
:deep(.credential-cell > div) {
  display: flex;
  flex-direction: column;
}
:deep(.credential-cell strong) {
  font-size: 12px;
}
:deep(.credential-cell small) {
  margin-top: 3px;
  color: #657281;
  font-size: 9px;
}
:deep(.fingerprint) {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #7e8b99;
  font-size: 9px;
}
:deep(.row-actions) {
  display: flex;
  justify-content: flex-end;
  gap: 3px;
}
.credential-modal {
  width: min(650px, calc(100vw - 30px));
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
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
:deep(.secret-input textarea) {
  font-family: 'JetBrains Mono', Consolas, monospace !important;
  font-size: 10px !important;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
@media (max-width: 600px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
