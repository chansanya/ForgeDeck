<!-- 运行详情页：展示阶段状态并通过可恢复 SSE 持续读取流水线日志。 -->
<template>
  <div class="page">
    <NButton text class="back-button" @click="router.push({ name: 'pipelines' })"
      ><template #icon><NIcon :component="ArrowLeft" /></template>返回流水线</NButton
    >
    <div v-if="loading && !run" class="loading-block"><NSpin size="large" /></div>
    <template v-else-if="run">
      <PageHeader
        :eyebrow="`RUN / ${run.id.slice(0, 12).toUpperCase()}`"
        :title="projectName || `运行 #${run.id.slice(0, 8)}`"
        :description="`${run.ref} · ${run.snapshot_sha256.slice(0, 12)}`"
      >
        <template #actions
          ><StatusBadge :status="run.status" /><NButton
            v-if="!terminal"
            type="error"
            secondary
            @click="cancelRun"
            ><template #icon><NIcon :component="Ban" /></template>取消运行</NButton
          ><NButton secondary @click="load()"
            ><template #icon><NIcon :component="RefreshCw" /></template>刷新</NButton
          ></template
        >
      </PageHeader>

      <section class="run-facts panel">
        <div>
          <span>COMMIT</span
          ><strong class="mono"
            ><GitCommitHorizontal :size="14" />{{ shortSha(run.commit_sha) }}</strong
          ><small>{{ run.ref }}</small>
        </div>
        <div>
          <span>TRIGGER</span><strong class="mono">{{ run.trigger_type.toUpperCase() }}</strong
          ><small>{{ formatFullDate(run.created_at) }}</small>
        </div>
        <div>
          <span>DURATION</span
          ><strong class="mono">{{
            formatDuration(durationSeconds(run.started_at, run.finished_at))
          }}</strong
          ><small>{{ run.current_stage || '等待开始' }}</small>
        </div>
        <div>
          <span>IMAGE DIGEST</span
          ><strong class="mono digest">{{
            run.image_digest?.replace('sha256:', '').slice(0, 16) || '—'
          }}</strong
          ><small>{{ run.environment_id || '仅构建' }}</small>
        </div>
      </section>

      <section class="pipeline panel">
        <div class="section-heading">
          <div>
            <p class="eyebrow">EXECUTION GRAPH</p>
            <h2>阶段状态</h2>
          </div>
        </div>
        <div class="stage-track">
          <div v-for="stage in stages" :key="stage" class="stage" :data-state="stageState(stage)">
            <span class="stage-node"
              ><Check v-if="stageState(stage) === 'done'" :size="14" /><X
                v-else-if="stageState(stage) === 'failed'"
                :size="14" /><RotateCcw
                v-else-if="stageState(stage) === 'active'"
                :size="14" /><Circle v-else :size="11"
            /></span>
            <div>
              <b>{{ stageLabels[stage] }}</b
              ><small class="mono">{{ stage.toUpperCase() }}</small>
            </div>
          </div>
        </div>
      </section>

      <section class="terminal-panel panel">
        <header class="terminal-header">
          <div>
            <span class="terminal-icon"><TerminalSquare :size="16" /></span>
            <div>
              <b>实时日志</b
              ><small
                ><Radio :size="10" />{{
                  streamState === 'live' ? 'LIVE' : streamState.toUpperCase()
                }}</small
              >
            </div>
          </div>
          <div class="terminal-actions">
            <label><input v-model="autoScroll" type="checkbox" /> 自动滚动</label
            ><NButton quaternary size="small" @click="copyLogs"
              ><template #icon><NIcon :component="Copy" /></template></NButton
            ><NButton quaternary size="small" @click="downloadLogs"
              ><template #icon><NIcon :component="Download" /></template
            ></NButton>
          </div>
        </header>
        <div
          ref="logRoot"
          class="log-output"
          role="log"
          aria-live="polite"
          aria-label="流水线实时日志"
        >
          <div v-if="!logs.length" class="log-placeholder">
            <span class="cursor" /> 正在等待 Runner 输出...
          </div>
          <div
            v-for="(entry, index) in logs"
            :key="entry.id || index"
            class="log-line"
            :data-stream="entry.stream || 'system'"
          >
            <span class="log-time">{{
              entry.timestamp
                ? new Date(entry.timestamp).toLocaleTimeString('zh-CN', { hour12: false })
                : '--:--:--'
            }}</span
            ><span v-if="entry.stage" class="log-stage">{{ entry.stage }}</span
            ><span class="log-message">{{ entry.message }}</span>
          </div>
        </div>
      </section>
      <div v-if="run.error_message" class="error-banner">
        <X :size="17" />
        <div>
          <b>运行失败</b>
          <p>{{ run.error_message }}</p>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NIcon, NSpin, useDialog, useMessage } from 'naive-ui'
import {
  ArrowLeft,
  Ban,
  Check,
  Circle,
  Copy,
  Download,
  GitCommitHorizontal,
  Radio,
  RefreshCw,
  RotateCcw,
  TerminalSquare,
  X,
} from 'lucide-vue-next'
import { api, streamRunEvents } from '@/api/client'
import type { PipelineRun, RunLogEvent } from '@/api/types'
import PageHeader from '@/components/PageHeader.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { durationSeconds, formatDuration, formatFullDate, shortSha } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const run = ref<PipelineRun | null>(null)
const projectName = ref('')
const loading = ref(true)
const logs = ref<RunLogEvent[]>([])
const logRoot = ref<HTMLElement | null>(null)
const streamState = ref<'connecting' | 'live' | 'closed' | 'error'>('connecting')
const autoScroll = ref(true)
let streamController: AbortController | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectAttempt = 0
let refreshTimer: ReturnType<typeof setInterval> | null = null
const runId = String(route.params.id)

const stages = ['checkout', 'build', 'registry', 'deploy', 'complete']
const stageLabels: Record<string, string> = {
  checkout: '检出源码',
  build: '构建并推送',
  registry: '固化镜像',
  deploy: '执行部署',
  complete: '完成验证',
}
const terminal = computed(() =>
  run.value
    ? ['succeeded', 'failed', 'cancelled', 'rolled_back'].includes(run.value.status)
    : false,
)

function stageState(stage: string): 'done' | 'active' | 'failed' | 'waiting' {
  /** 将后端阶段状态映射为时间线展示状态。 */
  if (!run.value) return 'waiting'
  const current = stages.indexOf(run.value.current_stage || '')
  const target = stages.indexOf(stage)
  if (target < current || run.value.status === 'succeeded') return 'done'
  if (target === current && run.value.status === 'failed') return 'failed'
  if (target === current && ['running', 'canceling', 'rolling_back'].includes(run.value.status))
    return 'active'
  return 'waiting'
}

async function load(silent = false): Promise<void> {
  /** 加载运行详情；SSE 重连期间可静默刷新避免重复提示。 */
  if (!silent) loading.value = true
  try {
    run.value = await api.runs.get(runId)
    if (!projectName.value) projectName.value = (await api.projects.get(run.value.project_id)).name
  } catch (error) {
    if (!silent) message.error(error instanceof Error ? error.message : '运行详情加载失败')
  } finally {
    loading.value = false
  }
}

function appendLog(event: RunLogEvent): void {
  /** 追加去重后的日志事件并保持终端滚动位置。 */
  if (event.id && logs.value.some((item) => item.id === event.id)) return
  logs.value.push(event)
  if (logs.value.length > 2500) logs.value.splice(0, logs.value.length - 2500)
  if (event.status && run.value) run.value.status = event.status
  if (autoScroll.value)
    void nextTick(() => {
      if (logRoot.value) logRoot.value.scrollTop = logRoot.value.scrollHeight
    })
}

async function connectLogs(): Promise<void> {
  /** 建立可恢复 SSE 日志流，断线后按最后事件序号重连。 */
  streamController?.abort()
  streamController = new AbortController()
  streamState.value = 'connecting'
  try {
    await streamRunEvents(runId, {
      signal: streamController.signal,
      lastEventId: logs.value.at(-1)?.id,
      onEvent: (event) => {
        streamState.value = 'live'
        reconnectAttempt = 0
        appendLog(event)
      },
    })
    streamState.value = 'closed'
  } catch (error) {
    if (streamController.signal.aborted) return
    streamState.value = 'error'
    if (!terminal.value && reconnectAttempt < 5) {
      reconnectAttempt += 1
      reconnectTimer = setTimeout(connectLogs, Math.min(1000 * 2 ** reconnectAttempt, 15000))
    } else if (!terminal.value)
      message.error(error instanceof Error ? error.message : '日志流已断开')
  }
}

function cancelRun(): void {
  /** 请求取消当前运行，最终状态仍以 Runner 持久状态为准。 */
  if (!run.value) return
  dialog.warning({
    title: '取消流水线',
    content: '将停止尚未完成的阶段。已推送的镜像不会自动删除。',
    positiveText: '确认取消',
    negativeText: '继续运行',
    onPositiveClick: async () => {
      try {
        run.value = await api.runs.cancel(runId)
        message.success('已提交取消请求')
      } catch (error) {
        message.error(error instanceof Error ? error.message : '取消失败')
      }
    },
  })
}

async function copyLogs(): Promise<void> {
  /** 将当前已加载日志复制到剪贴板。 */
  try {
    await navigator.clipboard.writeText(
      logs.value.map((item) => `[${item.timestamp || ''}] ${item.message}`).join('\n'),
    )
    message.success('日志已复制')
  } catch {
    message.error('无法访问剪贴板，请使用下载日志功能')
  }
}

function downloadLogs(): void {
  /** 将日志文本生成浏览器下载文件，不上传到服务端。 */
  const blob = new Blob(
    [
      logs.value
        .map((item) => `[${item.timestamp || ''}] [${item.stream || 'system'}] ${item.message}`)
        .join('\n'),
    ],
    { type: 'text/plain;charset=utf-8' },
  )
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = `run-${runId}.log`
  link.click()
  URL.revokeObjectURL(link.href)
}

onMounted(async () => {
  await load()
  void connectLogs()
  refreshTimer = setInterval(() => {
    if (!terminal.value) void load(true)
  }, 5000)
})
onBeforeUnmount(() => {
  streamController?.abort()
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.back-button {
  margin-bottom: 18px;
  color: #7f8b99;
}
.run-facts {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  margin-bottom: 16px;
}
.run-facts > div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  padding: 17px 20px;
  border-right: 1px solid #202a35;
}
.run-facts > div:last-child {
  border: 0;
}
.run-facts span {
  color: #5e6b79;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
  letter-spacing: 0.12em;
}
.run-facts strong {
  display: flex;
  align-items: center;
  gap: 7px;
  overflow: hidden;
  margin-top: 7px;
  color: #c3ced9;
  font-size: 13px;
  text-overflow: ellipsis;
}
.run-facts small {
  margin-top: 5px;
  color: #697685;
  font-size: 10px;
}
.run-facts .digest {
  color: #c7ff4a;
}
.pipeline {
  margin-bottom: 16px;
  padding: 20px;
}
.stage-track {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 0;
}
.stage {
  position: relative;
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}
.stage:not(:last-child)::after {
  position: absolute;
  z-index: 0;
  top: 18px;
  right: 0;
  left: 42px;
  height: 1px;
  background: #2a3440;
  content: '';
}
.stage-node {
  z-index: 1;
  display: grid;
  width: 36px;
  height: 36px;
  flex: 0 0 auto;
  border: 1px solid #313b48;
  border-radius: 10px;
  color: #606d7b;
  background: #10161e;
  place-items: center;
}
.stage div {
  z-index: 1;
  display: flex;
  min-width: 0;
  flex-direction: column;
  padding-right: 8px;
  background: #0f151e;
}
.stage b {
  font-size: 11px;
  white-space: nowrap;
}
.stage small {
  margin-top: 2px;
  color: #53606d;
  font-size: 7px;
  letter-spacing: 0.08em;
}
.stage[data-state='done'] .stage-node {
  border-color: rgba(80, 216, 144, 0.4);
  color: #50d890;
  background: rgba(80, 216, 144, 0.08);
}
.stage[data-state='active'] .stage-node {
  border-color: rgba(199, 255, 74, 0.45);
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.08);
  animation: pulse 1.5s infinite;
}
.stage[data-state='failed'] .stage-node {
  border-color: rgba(255, 99, 125, 0.4);
  color: #ff637d;
  background: rgba(255, 99, 125, 0.08);
}
@keyframes pulse {
  50% {
    box-shadow: 0 0 18px rgba(199, 255, 74, 0.14);
  }
}
.terminal-panel {
  overflow: hidden;
}
.terminal-header {
  display: flex;
  height: 54px;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px;
  border-bottom: 1px solid #202a35;
  background: #0b1016;
}
.terminal-header > div,
.terminal-header > div > div {
  display: flex;
  align-items: center;
  gap: 10px;
}
.terminal-icon {
  display: grid;
  width: 31px;
  height: 31px;
  border-radius: 8px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.07);
  place-items: center;
}
.terminal-header b {
  font-size: 11px;
}
.terminal-header small {
  display: flex;
  align-items: center;
  gap: 4px;
  color: #50d890;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
}
.terminal-actions label {
  color: #657281;
  font-size: 10px;
}
.terminal-actions input {
  accent-color: #c7ff4a;
}
.log-output {
  height: 430px;
  overflow: auto;
  padding: 15px 17px;
  background: #070a0e;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 11px;
  line-height: 1.65;
}
.log-line {
  display: grid;
  grid-template-columns: 72px 90px minmax(0, 1fr);
  gap: 8px;
  min-height: 18px;
}
.log-line[data-stream='stderr'] .log-message {
  color: #ff8295;
}
.log-time {
  color: #46515d;
  user-select: none;
}
.log-stage {
  overflow: hidden;
  color: #8baa43;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.log-message {
  color: #aeb9c5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.log-placeholder {
  color: #53606e;
}
.cursor {
  display: inline-block;
  width: 6px;
  height: 12px;
  background: #c7ff4a;
  vertical-align: -2px;
  animation: blink 1s steps(1) infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.error-banner {
  display: flex;
  gap: 12px;
  margin-top: 16px;
  padding: 15px;
  border: 1px solid rgba(255, 99, 125, 0.3);
  border-radius: 12px;
  color: #ff637d;
  background: rgba(255, 99, 125, 0.06);
}
.error-banner b {
  font-size: 12px;
}
.error-banner p {
  margin: 4px 0 0;
  color: #ca7887;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  line-height: 1.5;
}
.stage-track {
  grid-template-columns: repeat(5, 1fr);
}
@media (max-width: 1100px) {
  .run-facts {
    grid-template-columns: repeat(2, 1fr);
  }
  .run-facts > div:nth-child(2) {
    border-right: 0;
  }
  .run-facts > div:nth-child(-n + 2) {
    border-bottom: 1px solid #202a35;
  }
  .stage-track {
    grid-template-columns: repeat(3, 1fr);
    gap: 15px;
  }
  .stage::after {
    display: none;
  }
}
@media (max-width: 650px) {
  .run-facts {
    grid-template-columns: 1fr;
  }
  .run-facts > div {
    border-right: 0;
    border-bottom: 1px solid #202a35 !important;
  }
  .stage-track {
    grid-template-columns: 1fr;
  }
  .log-line {
    grid-template-columns: 62px minmax(0, 1fr);
  }
  .log-stage {
    display: none;
  }
  .terminal-actions label {
    display: none;
  }
}
</style>
