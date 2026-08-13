<!-- 脚本库页面：维护版本化 SSH 脚本并提交受审批的执行申请。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="AUTOMATION LIBRARY"
      title="部署脚本"
      description="保存版本化的受控 Shell 脚本。参数以数组传递，平台不拿字符串拼 shell 命令玩俄罗斯轮盘。"
    >
      <template #actions
        ><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ><NButton type="primary" @click="create"
          ><template #icon><NIcon :component="Plus" /></template>新建脚本</NButton
        ></template
      >
    </PageHeader>
    <section class="panel table-panel">
      <NDataTable
        v-if="scripts.length || loading"
        :columns="columns"
        :data="scripts"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :scroll-x="950"
      /><EmptyState
        v-else
        :icon="FileCode2"
        title="脚本库为空"
        description="添加幂等、可重试的部署辅助脚本，别把半本运维手册硬塞进一条 SSH 命令。"
        action-label="新建脚本"
        @action="create"
      />
    </section>
    <NModal
      v-model:show="showEditor"
      preset="card"
      :title="editingId ? '编辑脚本' : '新建脚本'"
      class="script-modal"
      :bordered="false"
      ><NForm ref="formRef" :model="scriptForm" :rules="rules" label-placement="top"
        ><NFormItem label="名称" path="name"
          ><NInput v-model:value="scriptForm.name" placeholder="restart-nginx" /></NFormItem
        ><NFormItem label="说明" path="description"
          ><NInput
            v-model:value="scriptForm.description"
            placeholder="说明输入、影响范围与幂等性" /></NFormItem
        ><NFormItem label="脚本内容" path="content"
          ><NInput v-model:value="scriptForm.content" type="textarea" :rows="15" class="code-input"
        /></NFormItem>
        <div class="switch-row">
          <span><b>启用脚本</b><small>所有执行请求都必须经过审批</small></span
          ><NSwitch v-model:value="scriptForm.enabled" /></div></NForm
      ><template #footer
        ><div class="modal-footer">
          <NButton @click="showEditor = false">取消</NButton
          ><NButton type="primary" :loading="saving" @click="save">保存版本</NButton>
        </div></template
      ></NModal
    >
    <NModal
      v-model:show="showExecute"
      preset="card"
      :title="`执行脚本 · ${executingScript?.name || ''}`"
      class="execute-modal"
      :bordered="false"
      ><NForm label-placement="top"
        ><NFormItem label="目标服务器"
          ><NSelect
            v-model:value="executeForm.server_id"
            placeholder="选择已启用服务器"
            :options="
              servers.map((s) => ({
                label: `${s.name} · ${s.host}`,
                value: s.id,
                disabled: !s.enabled,
              }))
            " /></NFormItem
        ><NFormItem label="参数（key=value，空格分隔）"
          ><NInput
            v-model:value="executeForm.args"
            class="mono"
            placeholder="target=app graceful=true" /></NFormItem></NForm
      ><template #footer
        ><div class="modal-footer">
          <NButton @click="showExecute = false">取消</NButton
          ><NButton type="primary" :loading="saving" @click="execute">提交执行</NButton>
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
  NSwitch,
  useDialog,
  useMessage,
  type DataTableColumns,
  type FormInst,
  type FormRules,
} from 'naive-ui'
import {
  FileCode2,
  MoreHorizontal,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Script, ScriptInput, Server } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import { formatDate } from '@/utils/format'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const scripts = ref<Script[]>([])
const servers = ref<Server[]>([])
const showEditor = ref(false)
const showExecute = ref(false)
const saving = ref(false)
const editingId = ref<string | null>(null)
const executingScript = ref<Script | null>(null)
const formRef = ref<FormInst | null>(null)
const scriptForm = reactive<ScriptInput>({
  name: '',
  description: '',
  content: '#!/usr/bin/env bash\nset -Eeuo pipefail\n\n',
  enabled: true,
})
const executeForm = reactive({ server_id: '', args: '' })
const rules: FormRules = {
  name: { required: true, message: '请输入脚本名称' },
  content: { required: true, message: '脚本内容不能为空' },
}

async function load(): Promise<void> {
  /** 加载脚本版本列表并刷新当前编辑对象。 */
  loading.value = true
  try {
    ;[scripts.value, servers.value] = await Promise.all([api.scripts.list(), api.servers.list()])
  } catch (error) {
    message.error(error instanceof Error ? error.message : '脚本库加载失败')
  } finally {
    loading.value = false
  }
}

function create(): void {
  /** 初始化新脚本表单。 */
  editingId.value = null
  Object.assign(scriptForm, {
    name: '',
    description: '',
    content: '#!/usr/bin/env bash\nset -Eeuo pipefail\n\n',
    enabled: true,
  })
  showEditor.value = true
}
function edit(row: Script): void {
  /** 复制脚本内容到编辑表单，避免列表数据被直接改写。 */
  editingId.value = row.id
  Object.assign(scriptForm, {
    name: row.name,
    description: row.description || '',
    content: row.content || '',
    enabled: row.enabled,
  })
  showEditor.value = true
}
function openExecute(row: Script): void {
  /** 打开脚本执行申请并预选当前脚本版本。 */
  executingScript.value = row
  Object.assign(executeForm, { server_id: '', args: '' })
  showExecute.value = true
}

async function save(): Promise<void> {
  /** 保存脚本内容，后端会重新计算不可变版本摘要。 */
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  saving.value = true
  try {
    editingId.value
      ? await api.scripts.update(editingId.value, scriptForm)
      : await api.scripts.create(scriptForm)
    message.success(editingId.value ? '脚本已更新并生成新版本' : '脚本已创建')
    showEditor.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '脚本保存失败')
  } finally {
    saving.value = false
  }
}

async function execute(): Promise<void> {
  /** 创建绑定脚本版本和服务器的执行审批申请。 */
  if (!executingScript.value || !executeForm.server_id) {
    message.warning('请选择执行服务器')
    return
  }
  saving.value = true
  const argumentsMap = Object.fromEntries(
    executeForm.args
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .map((item) => {
        const [key, ...rest] = item.split('=')
        return [key, rest.join('=')]
      }),
  )
  try {
    const result = await api.scripts.execute(executingScript.value.id, {
      server_id: executeForm.server_id,
      arguments: argumentsMap,
    })
    message.success(
      'state' in result && result.state === 'pending'
        ? '执行申请已进入审批中心'
        : '脚本已进入执行队列',
    )
    showExecute.value = false
  } catch (error) {
    message.error(error instanceof Error ? error.message : '脚本执行申请失败')
  } finally {
    saving.value = false
  }
}

function remove(row: Script): void {
  /** 在确认后删除脚本记录。 */
  dialog.warning({
    title: '删除脚本',
    content: `确定删除“${row.name}”？已产生的执行审计不会删除。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.scripts.remove(row.id)
        message.success('脚本已删除')
        await load()
      } catch (error) {
        message.error(error instanceof Error ? error.message : '删除失败')
      }
    },
  })
}

const columns: DataTableColumns<Script> = [
  {
    title: '脚本',
    key: 'name',
    minWidth: 220,
    render: (row) =>
      h('div', { class: 'script-cell' }, [
        h('span', [h(FileCode2, { size: 17 })]),
        h('div', [h('strong', row.name), h('small', row.description || '未填写说明')]),
      ]),
  },
  { title: '解释器', key: 'shell', width: 100, render: () => h('code', 'bash/sh') },
  { title: '版本', key: 'version', width: 80, render: (row) => `v${row.current_version}` },
  {
    title: 'SHA-256',
    key: 'sha',
    minWidth: 140,
    render: (row) => h('code', row.sha256.slice(0, 12)),
  },
  {
    title: '审批',
    key: 'approval',
    width: 110,
    render: () => h('span', { class: 'approval yes' }, [h(ShieldCheck, { size: 13 }), '需要']),
  },
  { title: '更新', key: 'updated', width: 140, render: (row) => formatDate(row.updated_at) },
  {
    title: '',
    key: 'actions',
    width: 160,
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          { size: 'small', type: 'primary', secondary: true, onClick: () => openExecute(row) },
          { icon: () => h(NIcon, { component: Play }), default: () => '执行' },
        ),
        h(
          NButton,
          { circle: true, quaternary: true, onClick: () => edit(row) },
          { icon: () => h(NIcon, { component: MoreHorizontal }) },
        ),
        h(
          NButton,
          { circle: true, quaternary: true, type: 'error', onClick: () => remove(row) },
          { icon: () => h(NIcon, { component: Trash2 }) },
        ),
      ]),
  },
]
onMounted(load)
</script>

<style scoped>
.table-panel {
  padding-top: 4px;
}
:deep(.script-cell) {
  display: flex;
  align-items: center;
  gap: 10px;
}
:deep(.script-cell > span) {
  display: grid;
  width: 35px;
  height: 35px;
  border-radius: 9px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.07);
  place-items: center;
}
:deep(.script-cell > div) {
  display: flex;
  flex-direction: column;
}
:deep(.script-cell strong) {
  font-size: 12px;
}
:deep(.script-cell small) {
  margin-top: 3px;
  color: #657281;
  font-size: 10px;
}
:deep(.approval) {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #768392;
  font-size: 10px;
}
:deep(.approval.yes) {
  color: #f5b942;
}
:deep(.row-actions) {
  display: flex;
  justify-content: flex-end;
  gap: 3px;
}
.script-modal {
  width: min(850px, calc(100vw - 30px));
}
.execute-modal {
  width: min(540px, calc(100vw - 30px));
}
.form-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 14px;
}
:deep(.code-input textarea) {
  font-family: 'JetBrains Mono', Consolas, monospace !important;
  font-size: 11px !important;
  line-height: 1.65 !important;
  tab-size: 2;
}
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  align-self: center;
  padding: 11px 13px;
  border: 1px solid #26313c;
  border-radius: 10px;
}
.switch-row span {
  display: flex;
  flex-direction: column;
}
.switch-row b {
  font-size: 11px;
}
.switch-row small {
  margin-top: 3px;
  color: #63707f;
  font-size: 9px;
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
