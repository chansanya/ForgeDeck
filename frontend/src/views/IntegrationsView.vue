<!-- 集成页面：配置通知通道并签发最小权限的短期 MCP Token。 -->
<template>
  <div class="page">
    <PageHeader
      eyebrow="EXTERNAL INTERFACES"
      title="通知与 MCP"
      description="把结果送到该去的地方，也让 AI 以受控、可审计的方式观察和申请操作。绝不暴露任意 SSH 命令这种裸奔接口。"
    >
      <template #actions
        ><NButton secondary :loading="loading" @click="load"
          ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
        ></template
      >
    </PageHeader>
    <section class="panel integration-panel">
      <NTabs type="line" animated>
        <NTabPane name="notifications" tab="通知通道"
          ><div class="tab-head">
            <div>
              <p class="eyebrow">EVENT DELIVERY</p>
              <h2>通知通道</h2>
            </div>
            <NButton type="primary" @click="openNotification"
              ><template #icon><NIcon :component="Plus" /></template>添加通道</NButton
            >
          </div>
          <NDataTable
            v-if="notifications.length || loading"
            :data="notifications"
            :columns="notificationColumns"
            :loading="loading"
            :bordered="false"
            :single-line="false"
            :scroll-x="850" /><EmptyState
            v-else
            :icon="BellRing"
            title="没有通知通道"
            description="配置钉钉、签名 Webhook 或 SMTP，及时接收失败和部署结果。"
            action-label="添加通道"
            @action="openNotification"
        /></NTabPane>
        <NTabPane name="mcp" tab="MCP 访问"
          ><div class="tab-head">
            <div>
              <p class="eyebrow">MODEL CONTEXT PROTOCOL</p>
              <h2>AI 访问令牌</h2>
            </div>
            <NButton type="primary" @click="openToken"
              ><template #icon><NIcon :component="Plus" /></template>创建 Token</NButton
            >
          </div>
          <NAlert type="info" :bordered="false" class="mcp-alert"
            ><template #icon><Bot :size="18" /></template>MCP 地址为 <code>{{ mcpEndpoint }}</code
            >。写操作只会创建审批申请，不能直接执行任意命令。</NAlert
          ><NDataTable
            v-if="tokens.length || loading"
            :data="tokens"
            :columns="tokenColumns"
            :loading="loading"
            :bordered="false"
            :single-line="false"
            :scroll-x="1000" /><EmptyState
            v-else
            :icon="Bot"
            title="没有 MCP Token"
            description="为可信 AI 客户端签发短期、最小权限 Token。"
            action-label="创建 Token"
            @action="openToken"
        /></NTabPane>
      </NTabs>
    </section>

    <NModal
      v-model:show="showNotification"
      preset="card"
      title="添加通知通道"
      class="integration-modal"
      :bordered="false"
      ><NForm label-placement="top"
        ><div class="form-grid">
          <NFormItem label="名称"
            ><NInput v-model:value="notificationForm.name" placeholder="生产告警" /></NFormItem
          ><NFormItem label="类型"
            ><NSelect
              v-model:value="notificationForm.kind"
              :options="[
                { label: '钉钉机器人', value: 'dingtalk' },
                { label: '签名 Webhook', value: 'webhook' },
                { label: 'SMTP 邮件', value: 'smtp' },
              ]"
          /></NFormItem>
        </div>
        <template v-if="notificationForm.kind !== 'smtp'"
          ><NFormItem label="Webhook 地址"
            ><NInput v-model:value="notificationForm.target" placeholder="https://..." /></NFormItem
          ><NFormItem
            :label="notificationForm.kind === 'webhook' ? 'HMAC 签名密钥' : '钉钉加签密钥（可选）'"
            ><NInput
              v-model:value="notificationForm.secret"
              type="password"
              show-password-on="mousedown" /></NFormItem></template
        ><template v-else
          ><div class="form-grid">
            <NFormItem label="SMTP 主机"
              ><NInput
                v-model:value="notificationForm.smtpHost"
                placeholder="smtp.example.com" /></NFormItem
            ><NFormItem label="端口"
              ><NInputNumber v-model:value="notificationForm.smtpPort" :min="1" :max="65535"
            /></NFormItem>
          </div>
          <NFormItem label="发件人"
            ><NInput
              v-model:value="notificationForm.smtpSender"
              placeholder="devops@example.com" /></NFormItem
          ><NFormItem label="收件人（逗号分隔）"
            ><NInput
              v-model:value="notificationForm.smtpRecipients"
              placeholder="ops@example.com, owner@example.com"
          /></NFormItem>
          <div class="form-grid">
            <NFormItem label="用户名（可选）"
              ><NInput v-model:value="notificationForm.smtpUsername" /></NFormItem
            ><NFormItem label="密码（可选）"
              ><NInput
                v-model:value="notificationForm.secret"
                type="password"
                show-password-on="mousedown"
            /></NFormItem>
          </div>
          <div class="switch-row">
            <span>使用 STARTTLS</span
            ><NSwitch v-model:value="notificationForm.starttls" /></div></template
        ><NFormItem label="事件"
          ><NSelect
            v-model:value="notificationForm.events"
            multiple
            :options="[
              { label: '流水线失败', value: 'run.failed' },
              { label: '流水线成功', value: 'run.succeeded' },
              { label: '部署失败', value: 'deployment.failed' },
              { label: '部署成功', value: 'deployment.succeeded' },
              { label: '待审批', value: 'approval.pending' },
            ]"
        /></NFormItem>
        <div class="switch-row">
          <span>启用此通知通道</span
          ><NSwitch v-model:value="notificationForm.enabled" /></div></NForm
      ><template #footer
        ><div class="modal-footer">
          <NButton @click="showNotification = false">取消</NButton
          ><NButton type="primary" :loading="saving" @click="saveNotification"
            ><template #icon><NIcon :component="Send" /></template>保存通道</NButton
          >
        </div></template
      ></NModal
    >
    <NModal
      v-model:show="showToken"
      preset="card"
      title="创建 MCP Token"
      class="integration-modal"
      :bordered="false"
      ><NForm label-placement="top"
        ><NFormItem label="名称"
          ><NInput v-model:value="tokenForm.name" placeholder="codex-observer" /></NFormItem
        ><NFormItem label="权限范围"
          ><NSelect
            v-model:value="tokenForm.scopes"
            multiple
            :options="[
              { label: '读取状态', value: 'read:status' },
              { label: '读取日志', value: 'read:logs' },
              { label: '创建构建申请', value: 'request:build' },
              { label: '创建部署申请', value: 'request:deploy' },
              { label: '创建回滚申请', value: 'request:rollback' },
              { label: '创建脚本申请', value: 'request:script' },
            ]" /></NFormItem
        ><NFormItem label="有效期（秒）"
          ><NInputNumber
            v-model:value="tokenForm.expires_in_seconds"
            :min="300"
            :max="2592000" /></NFormItem></NForm
      ><template #footer
        ><div class="modal-footer">
          <NButton @click="showToken = false">取消</NButton
          ><NButton type="primary" :loading="saving" @click="createToken">生成 Token</NButton>
        </div></template
      ></NModal
    >
    <NModal
      :show="Boolean(newToken)"
      preset="card"
      title="Token 已创建"
      class="token-modal"
      :bordered="false"
      :mask-closable="false"
      @update:show="
        (value) => {
          if (!value) newToken = null
        }
      "
      ><NAlert type="warning" :bordered="false"
        >Token 只显示一次。关闭前请复制并存入安全位置。</NAlert
      >
      <div class="token-value">
        <code>{{ newToken }}</code
        ><NButton type="primary" @click="copyToken"
          ><template #icon><NIcon :component="Copy" /></template>复制</NButton
        >
      </div>
      <template #footer
        ><NButton block @click="newToken = null">我已保存</NButton></template
      ></NModal
    >
  </div>
</template>

<script setup lang="ts">
import { h, onMounted, reactive, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NDataTable,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
  NModal,
  NSelect,
  NSwitch,
  NTabPane,
  NTabs,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import { BellRing, Bot, Copy, KeyRound, Plus, RefreshCw, Send, Trash2 } from 'lucide-vue-next'
import { api } from '@/api/client'
import type {
  McpScope,
  McpToken,
  McpTokenInput,
  NotificationChannel,
  NotificationKind,
} from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatFullDate } from '@/utils/format'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const notifications = ref<NotificationChannel[]>([])
const tokens = ref<McpToken[]>([])
const showNotification = ref(false)
const showToken = ref(false)
const saving = ref(false)
const newToken = ref<string | null>(null)

interface NotificationFormState {
  name: string
  kind: NotificationKind
  target: string
  secret: string
  smtpHost: string
  smtpPort: number
  smtpSender: string
  smtpRecipients: string
  smtpUsername: string
  starttls: boolean
  enabled: boolean
  events: string[]
}

const notificationForm = reactive<NotificationFormState>({
  name: '',
  kind: 'dingtalk' as 'dingtalk' | 'webhook' | 'smtp',
  target: '',
  secret: '',
  smtpHost: '',
  smtpPort: 587,
  smtpSender: '',
  smtpRecipients: '',
  smtpUsername: '',
  starttls: true,
  enabled: true,
  events: ['run.failed', 'deployment.failed'],
})
const tokenForm = reactive<McpTokenInput>({
  name: '',
  scopes: ['read:status'],
  expires_in_seconds: 86400,
})
const mcpEndpoint = `${window.location.origin}/mcp`

function resetNotificationForm(): void {
  /** 清空通知渠道表单并恢复默认类型。 */
  Object.assign(notificationForm, {
    name: '',
    kind: 'dingtalk' as NotificationKind,
    target: '',
    secret: '',
    smtpHost: '',
    smtpPort: 587,
    smtpSender: '',
    smtpRecipients: '',
    smtpUsername: '',
    starttls: true,
    enabled: true,
    events: ['run.failed', 'deployment.failed'],
  })
}

function openNotification(): void {
  /** 打开新通知渠道编辑器。 */
  resetNotificationForm()
  showNotification.value = true
}

function openToken(): void {
  /** 打开 MCP 短期令牌创建表单。 */
  Object.assign(tokenForm, {
    name: '',
    scopes: ['read:status'] as McpScope[],
    expires_in_seconds: 86400,
  })
  showToken.value = true
}

async function load(): Promise<void> {
  /** 加载通知渠道和 MCP 令牌安全元数据。 */
  loading.value = true
  try {
    ;[notifications.value, tokens.value] = await Promise.all([
      api.notifications.list(),
      api.mcp.tokens(),
    ])
  } catch (error) {
    message.error(error instanceof Error ? error.message : '集成配置加载失败')
  } finally {
    loading.value = false
  }
}
async function saveNotification(): Promise<void> {
  /** 保存通知渠道配置，敏感字段只在提交时发送。 */
  if (!notificationForm.name) {
    message.warning('请填写通道名称')
    return
  }
  if (!notificationForm.events.length) {
    message.warning('请至少选择一个通知事件')
    return
  }
  let config: Record<string, unknown>
  if (notificationForm.kind === 'smtp') {
    const recipients = notificationForm.smtpRecipients
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
    if (!notificationForm.smtpHost || !notificationForm.smtpSender || !recipients.length) {
      message.warning('SMTP 需要主机、发件人和至少一个收件人')
      return
    }
    config = {
      host: notificationForm.smtpHost,
      port: notificationForm.smtpPort,
      sender: notificationForm.smtpSender,
      recipients,
      username: notificationForm.smtpUsername || undefined,
      password: notificationForm.secret || undefined,
      starttls: notificationForm.starttls,
    }
  } else {
    if (!notificationForm.target) {
      message.warning('请填写 Webhook 地址')
      return
    }
    if (notificationForm.kind === 'webhook' && !notificationForm.secret) {
      message.warning('签名 Webhook 必须配置 HMAC 密钥')
      return
    }
    config =
      notificationForm.kind === 'dingtalk'
        ? { webhook_url: notificationForm.target, secret: notificationForm.secret || undefined }
        : { url: notificationForm.target, secret: notificationForm.secret }
  }
  saving.value = true
  try {
    await api.notifications.create({
      name: notificationForm.name,
      kind: notificationForm.kind,
      enabled: notificationForm.enabled,
      events: notificationForm.events,
      config,
    })
    message.success('通知通道已创建')
    showNotification.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '通知通道创建失败')
  } finally {
    saving.value = false
  }
}
async function testNotification(id: string): Promise<void> {
  /** 请求后端发送一次测试通知并展示结果。 */
  try {
    await api.notifications.test(id)
    message.success('测试通知已发送')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '测试发送失败')
  }
}
function removeNotification(row: NotificationChannel): void {
  /** 在确认后删除通知渠道。 */
  dialog.warning({
    title: '删除通知通道',
    content: `确定删除“${row.name}”？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.notifications.remove(row.id)
        await load()
      } catch (error) {
        message.error(error instanceof Error ? error.message : '删除失败')
      }
    },
  })
}
async function createToken(): Promise<void> {
  /** 创建短期 MCP 令牌并只在生成时展示一次明文。 */
  if (!tokenForm.name || !tokenForm.scopes.length) {
    message.warning('请填写名称并选择权限')
    return
  }
  saving.value = true
  try {
    const token = await api.mcp.createToken({
      name: tokenForm.name.trim(),
      scopes: [...tokenForm.scopes],
      expires_in_seconds: tokenForm.expires_in_seconds,
    })
    newToken.value = token.token || null
    showToken.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : 'Token 创建失败')
  } finally {
    saving.value = false
  }
}
function revokeToken(row: McpToken): void {
  /** 撤销 MCP 令牌，使后续请求立即失效。 */
  if (row.revoked_at) return
  dialog.warning({
    title: '吊销 MCP Token',
    content: `吊销“${row.name}”后，使用它的 AI 客户端会立即失去权限。`,
    positiveText: '确认吊销',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.mcp.revokeToken(row.id)
        message.success('Token 已吊销')
        await load()
      } catch (error) {
        message.error(error instanceof Error ? error.message : '吊销失败')
      }
    },
  })
}
async function copyToken(): Promise<void> {
  /** 将刚生成的令牌复制到系统剪贴板，不持久化令牌内容。 */
  if (!newToken.value) return
  try {
    await navigator.clipboard.writeText(newToken.value)
    message.success('Token 已复制')
  } catch {
    message.error('无法访问剪贴板，请手动复制 Token')
  }
}

function tokenStatus(row: McpToken): 'active' | 'expired' | 'revoked' {
  if (row.revoked_at) return 'revoked'
  return Date.parse(row.expires_at) <= Date.now() ? 'expired' : 'active'
}

const notificationColumns: DataTableColumns<NotificationChannel> = [
  {
    title: '通道',
    key: 'name',
    minWidth: 190,
    render: (row) =>
      h('div', { class: 'integration-name' }, [
        h('span', [h(BellRing, { size: 16 })]),
        h('div', [h('strong', row.name), h('small', row.kind.toUpperCase())]),
      ]),
  },
  { title: '目标', key: 'target', minWidth: 200, render: (row) => row.target_hint || '已加密' },
  {
    title: '事件',
    key: 'events',
    minWidth: 220,
    render: (row) =>
      h(
        'div',
        { class: 'scope-list' },
        row.events.map((event) => h('code', event)),
      ),
  },
  {
    title: '状态',
    key: 'enabled',
    width: 100,
    render: (row) => (row.enabled ? '已启用' : '已停用'),
  },
  {
    title: '',
    key: 'actions',
    width: 120,
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          { size: 'small', secondary: true, onClick: () => testNotification(row.id) },
          { default: () => '测试' },
        ),
        h(
          NButton,
          { quaternary: true, circle: true, type: 'error', onClick: () => removeNotification(row) },
          { icon: () => h(NIcon, { component: Trash2 }) },
        ),
      ]),
  },
]
const tokenColumns: DataTableColumns<McpToken> = [
  {
    title: 'Token',
    key: 'name',
    minWidth: 190,
    render: (row) =>
      h('div', { class: 'integration-name' }, [
        h('span', [h(KeyRound, { size: 16 })]),
        h('div', [h('strong', row.name), h('small', row.id.slice(0, 12))]),
      ]),
  },
  {
    title: '权限',
    key: 'scopes',
    minWidth: 260,
    render: (row) =>
      h(
        'div',
        { class: 'scope-list' },
        row.scopes.map((scope) => h('code', scope)),
      ),
  },
  {
    title: '过期时间',
    key: 'expires',
    width: 180,
    render: (row) => formatFullDate(row.expires_at),
  },
  { title: '最近使用', key: 'used', width: 180, render: (row) => formatFullDate(row.last_used_at) },
  {
    title: '状态',
    key: 'status',
    width: 100,
    render: (row) => h(StatusBadge, { status: tokenStatus(row) }),
  },
  {
    title: '',
    key: 'actions',
    width: 60,
    render: (row) =>
      h(
        NButton,
        {
          quaternary: true,
          circle: true,
          type: 'error',
          disabled: Boolean(row.revoked_at),
          title: row.revoked_at ? 'Token 已吊销' : '吊销 Token',
          onClick: () => revokeToken(row),
        },
        { icon: () => h(NIcon, { component: Trash2 }) },
      ),
  },
]
watch(showNotification, (visible) => {
  if (!visible) resetNotificationForm()
})
watch(
  () => notificationForm.kind,
  () => {
    notificationForm.secret = ''
  },
)
onMounted(load)
</script>

<style scoped>
.integration-panel {
  padding: 13px 18px 18px;
}
.tab-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 4px 0 15px;
}
.tab-head h2 {
  margin: 0;
  font-size: 17px;
}
.mcp-alert {
  margin-bottom: 15px;
}
.mcp-alert code {
  color: #c7ff4a;
}
:deep(.integration-name) {
  display: flex;
  align-items: center;
  gap: 10px;
}
:deep(.integration-name > span) {
  display: grid;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.07);
  place-items: center;
}
:deep(.integration-name > div) {
  display: flex;
  flex-direction: column;
}
:deep(.integration-name strong) {
  font-size: 11px;
}
:deep(.integration-name small) {
  margin-top: 3px;
  color: #62707f;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
}
:deep(.scope-list) {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
:deep(.scope-list code) {
  padding: 3px 6px;
  border-radius: 5px;
  color: #8795a4;
  background: #111923;
  font-size: 8px;
}
:deep(.row-actions) {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
}
.integration-modal,
.token-modal {
  width: min(600px, calc(100vw - 30px));
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 13px;
  border: 1px solid #26313c;
  border-radius: 9px;
  color: #8c99a7;
  font-size: 11px;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
.token-value {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 15px;
  padding: 12px;
  border: 1px solid #283440;
  border-radius: 9px;
  background: #080c11;
}
.token-value code {
  min-width: 0;
  flex: 1;
  overflow-wrap: anywhere;
  color: #c7ff4a;
  font-size: 10px;
}
@media (max-width: 600px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
