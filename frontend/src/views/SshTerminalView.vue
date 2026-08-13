<!-- SSH 终端页：使用一次性会话票据承载实时 PTY，不持久化终端内容。 -->
<template>
  <div class="page terminal-page">
    <PageHeader
      eyebrow="INTERACTIVE SESSION"
      title="SSH 终端"
      description="浏览器仅承载实时 PTY 数据。平台记录会话元数据，不录制完整终端内容。"
    >
      <template #actions
        ><NSelect
          v-model:value="serverId"
          class="server-select"
          placeholder="选择服务器"
          :options="
            servers.map((s) => ({
              label: `${s.name} · ${s.host}`,
              value: s.id,
              disabled: serverStatus(s) !== 'online',
            }))
          "
          :disabled="state === 'connected' || state === 'connecting'"
        /><NButton
          v-if="state !== 'connected'"
          type="primary"
          :loading="state === 'connecting'"
          @click="connect"
          ><template #icon><NIcon :component="Plug" /></template>连接</NButton
        ><NButton v-else type="error" secondary @click="disconnect()"
          ><template #icon><NIcon :component="Power" /></template>断开</NButton
        ></template
      >
    </PageHeader>
    <section class="terminal-shell panel">
      <header>
        <div>
          <span class="traffic red" /><span class="traffic amber" /><span
            class="traffic green"
          /><span class="terminal-title"
            ><TerminalSquare :size="13" />{{
              servers.find((s) => s.id === serverId)?.name || 'NO SESSION'
            }}</span
          >
        </div>
        <div>
          <span class="connection" :data-state="state"><i />{{ state.toUpperCase() }}</span
          ><NButton quaternary circle size="small" title="重新连接" @click="connect"
            ><template #icon><NIcon :component="RefreshCw" /></template></NButton
          ><NButton quaternary circle size="small" title="全屏" @click="fullscreen"
            ><template #icon><NIcon :component="Maximize2" /></template
          ></NButton>
        </div>
      </header>
      <div ref="terminalRoot" class="xterm-root" aria-label="SSH 交互终端" />
      <footer>
        <span><ShieldCheck :size="12" />HOST KEY VERIFIED</span><span>UTF-8 · PTY</span
        ><span>SCROLLBACK 5000</span>
      </footer>
    </section>
    <div class="terminal-note">
      <ServerIcon :size="15" /><span
        >连接前会校验已保存的主机指纹；指纹变化时服务端必须拒绝会话，而不是弹个提示继续糊弄。</span
      >
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NIcon, NSelect, useMessage } from 'naive-ui'
import {
  Maximize2,
  Plug,
  Power,
  RefreshCw,
  Server as ServerIcon,
  ShieldCheck,
  TerminalSquare,
} from 'lucide-vue-next'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { api, resolveWebSocketUrl } from '@/api/client'
import type { Server } from '@/api/types'
import PageHeader from '@/components/PageHeader.vue'
import { serverStatus } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const servers = ref<Server[]>([])
const serverId = ref<string | null>((route.params.serverId as string | undefined) || null)
const terminalRoot = ref<HTMLDivElement | null>(null)
const state = ref<'idle' | 'connecting' | 'connected' | 'closed' | 'error'>('idle')
let terminal: Terminal | null = null
let fitAddon: FitAddon | null = null
let socket: WebSocket | null = null
let resizeObserver: ResizeObserver | null = null

async function loadServers(): Promise<void> {
  /** 加载可用服务器供终端连接选择。 */
  try {
    servers.value = await api.servers.list()
    serverId.value ||=
      servers.value.find((item) => serverStatus(item) === 'online')?.id ||
      servers.value[0]?.id ||
      null
  } catch (error) {
    message.error(error instanceof Error ? error.message : '服务器加载失败')
  }
}

function initializeTerminal(): void {
  /** 创建 xterm 实例并绑定窗口尺寸和输入输出事件。 */
  if (!terminalRoot.value || terminal) return
  terminal = new Terminal({
    cursorBlink: true,
    cursorStyle: 'bar',
    fontFamily: "'JetBrains Mono', Consolas, monospace",
    fontSize: 13,
    lineHeight: 1.35,
    scrollback: 5000,
    allowProposedApi: false,
    theme: {
      background: '#06090d',
      foreground: '#b7c2cd',
      cursor: '#c7ff4a',
      cursorAccent: '#080b10',
      selectionBackground: '#34431f',
      black: '#151a20',
      red: '#ff637d',
      green: '#8adf72',
      yellow: '#f5b942',
      blue: '#5ea1ff',
      magenta: '#c987ff',
      cyan: '#64d7d0',
      white: '#dce5ee',
      brightBlack: '#56616d',
    },
  })
  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.open(terminalRoot.value)
  fitAddon.fit()
  terminal.onData((data) => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data }))
  })
  resizeObserver = new ResizeObserver(() => {
    fitAddon?.fit()
    if (socket?.readyState === WebSocket.OPEN && terminal)
      socket.send(JSON.stringify({ type: 'resize', columns: terminal.cols, rows: terminal.rows }))
  })
  resizeObserver.observe(terminalRoot.value)
}

async function connect(): Promise<void> {
  /** 创建一次性 SSH 会话并连接 WebSocket PTY。 */
  if (!serverId.value) {
    message.warning('请选择服务器')
    return
  }
  disconnect(false)
  await nextTick()
  initializeTerminal()
  state.value = 'connecting'
  terminal?.reset()
  terminal?.writeln('\x1b[38;2;199;255;74mForgeDeck secure terminal\x1b[0m')
  terminal?.writeln('\x1b[90m正在申请一次性 SSH 会话...\x1b[0m\r\n')
  try {
    const session = await api.ssh.createSession(serverId.value)
    socket = new WebSocket(resolveWebSocketUrl(session))
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => {
      state.value = 'connected'
      if (terminal)
        socket?.send(
          JSON.stringify({ type: 'resize', columns: terminal.cols, rows: terminal.rows }),
        )
      terminal?.focus()
    }
    socket.onmessage = async (event) => {
      if (event.data instanceof Blob) {
        terminal?.write(new Uint8Array(await event.data.arrayBuffer()))
        return
      }
      if (event.data instanceof ArrayBuffer) {
        terminal?.write(new Uint8Array(event.data))
        return
      }
      const raw = String(event.data)
      try {
        const payload = JSON.parse(raw) as { type?: string; data?: string; message?: string }
        if (payload.type === 'error')
          terminal?.writeln(`\r\n\x1b[31m${payload.message || 'SSH 会话错误'}\x1b[0m`)
        else terminal?.write(payload.data || payload.message || '')
      } catch {
        terminal?.write(raw)
      }
    }
    socket.onerror = () => {
      state.value = 'error'
      terminal?.writeln('\r\n\x1b[31mWebSocket 连接错误\x1b[0m')
    }
    socket.onclose = (event) => {
      state.value = 'closed'
      terminal?.writeln(`\r\n\x1b[90m会话已关闭（${event.code}）\x1b[0m`)
    }
    await router.replace({ name: 'ssh', params: { serverId: serverId.value } })
  } catch (error) {
    state.value = 'error'
    terminal?.writeln(
      `\r\n\x1b[31m${error instanceof Error ? error.message : '会话创建失败'}\x1b[0m`,
    )
  }
}

function disconnect(print = true): void {
  /** 关闭终端 WebSocket 和 xterm 资源，按需打印断开提示。 */
  if (socket) {
    socket.onclose = null
    socket.close(1000, 'client disconnect')
    socket = null
  }
  if (print && state.value === 'connected') terminal?.writeln('\r\n\x1b[90m会话已由用户断开\x1b[0m')
  state.value = 'idle'
}

function fullscreen(): void {
  /** 请求终端容器进入浏览器全屏模式。 */
  void terminalRoot.value?.parentElement?.requestFullscreen()
  setTimeout(() => fitAddon?.fit(), 100)
}

onMounted(async () => {
  await loadServers()
  await nextTick()
  initializeTerminal()
  if (route.params.serverId) void connect()
})
onBeforeUnmount(() => {
  disconnect(false)
  resizeObserver?.disconnect()
  terminal?.dispose()
})
</script>

<style scoped>
.terminal-page {
  display: flex;
  min-height: calc(100vh - 58px);
  flex-direction: column;
}
.server-select {
  width: 260px;
}
.terminal-shell {
  display: flex;
  min-height: 600px;
  flex: 1;
  flex-direction: column;
  overflow: hidden;
  border-color: #29333f;
  background: #06090d;
}
.terminal-shell > header {
  display: flex;
  height: 46px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  padding: 0 13px;
  border-bottom: 1px solid #1e2731;
  background: #0d1219;
}
.terminal-shell > header > div {
  display: flex;
  align-items: center;
  gap: 7px;
}
.traffic {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}
.traffic.red {
  background: #ff637d;
}
.traffic.amber {
  background: #f5b942;
}
.traffic.green {
  background: #50d890;
}
.terminal-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: 10px;
  color: #697685;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.06em;
}
.connection {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #6d7987;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
}
.connection i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #586471;
}
.connection[data-state='connected'] {
  color: #50d890;
}
.connection[data-state='connected'] i {
  background: #50d890;
  box-shadow: 0 0 8px #50d890;
}
.connection[data-state='error'] {
  color: #ff637d;
}
.connection[data-state='connecting'] {
  color: #f5b942;
}
.xterm-root {
  min-height: 500px;
  flex: 1;
  padding: 13px 10px;
  background: #06090d;
}
:deep(.xterm) {
  height: 100%;
}
:deep(.xterm-viewport) {
  scrollbar-color: #26313c #06090d;
}
.terminal-shell > footer {
  display: flex;
  height: 28px;
  align-items: center;
  gap: 18px;
  padding: 0 12px;
  border-top: 1px solid #1b242d;
  color: #485460;
  background: #0a0f15;
  font-family: 'JetBrains Mono', monospace;
  font-size: 7px;
  letter-spacing: 0.07em;
}
.terminal-shell > footer span {
  display: flex;
  align-items: center;
  gap: 5px;
}
.terminal-shell > footer span:first-child {
  color: #668979;
}
.terminal-note {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 11px;
  color: #5f6c7a;
  font-size: 9px;
}
@media (max-width: 700px) {
  .server-select {
    width: 100%;
  }
  .terminal-shell {
    min-height: 500px;
  }
  .terminal-shell > footer span:nth-child(n + 2) {
    display: none;
  }
}
</style>
