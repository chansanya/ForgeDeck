/* 封装 REST、Bearer Token、可恢复 SSE 和 SSH 一次性会话请求。 */

import type {
  ApiList,
  Approval,
  AuditEvent,
  Credential,
  CredentialInput,
  DashboardSummary,
  Deployment,
  DockerAction,
  DockerActionInput,
  DockerActionResult,
  DockerOverview,
  EntityId,
  Environment,
  EnvironmentInput,
  HostKeyScanResult,
  LoginRequest,
  LoginResponse,
  McpToken,
  McpTokenInput,
  MetricPoint,
  NotificationChannel,
  NotificationChannelInput,
  PipelineRun,
  Project,
  ProjectInput,
  ProjectTemplate,
  RunLogEvent,
  RunRequest,
  RunStatus,
  Script,
  ScriptInput,
  Server,
  ServerInput,
  SshSession,
  User,
} from './types'

const TOKEN_KEY = 'devops_console_token'
const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export const tokenStorage = {
  get: () => sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY),
  set: (token: string, persistent = false) => {
    sessionStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(TOKEN_KEY)
    ;(persistent ? localStorage : sessionStorage).setItem(TOKEN_KEY, token)
  },
  clear: () => {
    sessionStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(TOKEN_KEY)
  },
}

function getErrorMessage(payload: unknown, fallback: string): string {
  /** 将 FastAPI 校验错误或普通文本统一转换为用户可读消息。 */
  if (typeof payload === 'string') return payload
  if (payload && typeof payload === 'object') {
    const detail = (payload as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (typeof item === 'object' && item && 'msg' in item ? String(item.msg) : String(item)))
        .join('；')
    }
    const message = (payload as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return fallback
}

function buildUrl(path: string, query?: Record<string, string | number | boolean | null | undefined>): string {
  /** 拼接 API 基地址和过滤参数，并忽略空查询值。 */
  const url = `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== '') params.set(key, String(value))
  }
  const encoded = params.toString()
  return encoded ? `${url}?${encoded}` : url
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  query?: Record<string, string | number | boolean | null | undefined>,
): Promise<T> {
  /** 注入认证头并解析响应；401 会清理本地会话触发重新登录。 */
  const token = tokenStorage.get()
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), { ...options, headers })
  } catch (error) {
    throw new ApiError(0, '无法连接 DevOps API，请检查服务状态和网络。', error)
  }

  const contentType = response.headers.get('content-type') || ''
  const payload: unknown = response.status === 204
    ? undefined
    : contentType.includes('application/json')
      ? await response.json().catch(() => undefined)
      : await response.text().catch(() => undefined)

  if (!response.ok) {
    if (response.status === 401) {
      tokenStorage.clear()
      window.dispatchEvent(new CustomEvent('devops:unauthorized'))
    }
    throw new ApiError(response.status, getErrorMessage(payload, `请求失败（${response.status}）`), payload)
  }
  return payload as T
}

function json(method: string, body?: unknown): RequestInit {
  /** 将请求方法和 JSON 请求体封装为浏览器 fetch 配置。 */
  return { method, body: body === undefined ? undefined : JSON.stringify(body) }
}

function list<T>(payload: T[] | ApiList<T>): T[] {
  /** 兼容后端直接数组和分页对象两种列表响应。 */
  return Array.isArray(payload) ? payload : payload.items
}

export const api = {
  auth: {
    login: (input: LoginRequest) => request<LoginResponse>('/auth/login', json('POST', input)),
    me: () => request<User>('/auth/me'),
  },
  dashboard: {
    summary: () => request<DashboardSummary>('/dashboard/summary'),
  },
  projects: {
    list: async () => list(await request<Project[] | ApiList<Project>>('/projects')),
    get: (id: EntityId) => request<Project>(`/projects/${id}`),
    create: (input: ProjectInput) => request<Project>('/projects', json('POST', input)),
    update: (id: EntityId, input: Partial<ProjectInput>) => request<Project>(`/projects/${id}`, json('PATCH', input)),
    remove: (id: EntityId) => request<void>(`/projects/${id}`, json('DELETE')),
    environments: async (id: EntityId) => list(await request<Environment[] | ApiList<Environment>>(`/projects/${id}/environments`)),
    createEnvironment: (id: EntityId, input: EnvironmentInput) => request<Environment>(`/projects/${id}/environments`, json('POST', input)),
    updateEnvironment: (projectId: EntityId, environmentId: EntityId, input: Partial<EnvironmentInput>) => request<Environment>(`/projects/${projectId}/environments/${environmentId}`, json('PATCH', input)),
    removeEnvironment: (projectId: EntityId, environmentId: EntityId) => request<void>(`/projects/${projectId}/environments/${environmentId}`, json('DELETE')),
    run: (id: EntityId, input: RunRequest) => request<PipelineRun>(`/projects/${id}/runs`, json('POST', input)),
  },
  templates: {
    list: async () => list(await request<ProjectTemplate[] | ApiList<ProjectTemplate>>('/templates')),
  },
  runs: {
    list: async (query?: { project_id?: string; status?: RunStatus; limit?: number }) => list(await request<PipelineRun[] | ApiList<PipelineRun>>('/runs', {}, query)),
    get: (id: EntityId) => request<PipelineRun>(`/runs/${id}`),
    cancel: (id: EntityId) => request<PipelineRun>(`/runs/${id}/cancel`, json('POST')),
  },
  servers: {
    list: async () => list(await request<Server[] | ApiList<Server>>('/servers')),
    get: (id: EntityId) => request<Server>(`/servers/${id}`),
    create: (input: ServerInput) => request<Server>('/servers', json('POST', input)),
    update: (id: EntityId, input: Partial<ServerInput>) => request<Server>(`/servers/${id}`, json('PATCH', input)),
    remove: (id: EntityId) => request<void>(`/servers/${id}`, json('DELETE')),
    metrics: async (id: EntityId, hours = 24) => list(await request<MetricPoint[] | ApiList<MetricPoint>>(`/servers/${id}/metrics`, {}, { hours })),
    scanHostKey: (host: string, port: number) => request<HostKeyScanResult>('/servers/host-key-scan', json('POST', { host, port })),
  },
  docker: {
    overview: (serverId: EntityId) => request<DockerOverview>(`/servers/${serverId}/docker/overview`),
    action: (serverId: EntityId, action: DockerAction, input: DockerActionInput) =>
      request<DockerActionResult>(`/servers/${serverId}/docker/actions/${action}`, json('POST', input)),
    containerAction: (serverId: EntityId, containerName: string, action: 'start' | 'stop' | 'restart') =>
      request<DockerActionResult>(`/servers/${serverId}/docker/actions/container_${action}`, json('POST', { target: containerName })),
    remove: (serverId: EntityId, kind: 'container' | 'image' | 'volume' | 'network', target: string, confirmation: string) =>
      request<DockerActionResult>(`/servers/${serverId}/docker/actions/${kind}_remove`, json('POST', { target, confirmation })),
    composeAction: (serverId: EntityId, action: 'up' | 'down' | 'restart', environmentId: EntityId, timeoutSeconds = 120) =>
      request<DockerActionResult>(`/servers/${serverId}/docker/actions/compose_${action}`, json('POST', {
        target: environmentId,
        options: { environment_id: environmentId, timeout_seconds: timeoutSeconds },
      })),
  },
  deployments: {
    list: async () => list(await request<Deployment[] | ApiList<Deployment>>('/deployments')),
    get: (id: EntityId) => request<Deployment>(`/deployments/${id}`),
    request: (input: { environment_id: EntityId; image_ref: string; image_digest: string; revision: string; compose_content?: string }) =>
      request<Approval | Deployment>('/deployments/requests', json('POST', input)),
    rollback: (id: EntityId) => request<Approval>(`/deployments/${id}/rollback-request`, json('POST')),
  },
  scripts: {
    list: async () => list(await request<Script[] | ApiList<Script>>('/scripts')),
    create: (input: ScriptInput) => request<Script>('/scripts', json('POST', input)),
    update: (id: EntityId, input: Partial<ScriptInput>) => request<Script>(`/scripts/${id}`, json('PATCH', input)),
    remove: (id: EntityId) => request<void>(`/scripts/${id}`, json('DELETE')),
    execute: (id: EntityId, input: { server_id: EntityId; arguments?: Record<string, string> }) =>
      request<Approval | { execution_id: EntityId }>(`/scripts/${id}/executions`, json('POST', input)),
  },
  credentials: {
    list: async () => list(await request<Credential[] | ApiList<Credential>>('/credentials')),
    create: (input: CredentialInput) => request<Credential>('/credentials', json('POST', input)),
    update: (id: EntityId, input: Partial<CredentialInput>) => request<Credential>(`/credentials/${id}`, json('PATCH', input)),
    remove: (id: EntityId) => request<void>(`/credentials/${id}`, json('DELETE')),
  },
  approvals: {
    list: async (state?: string) => list(await request<Approval[] | ApiList<Approval>>('/approvals', {}, { state })),
    approve: (id: EntityId, parameterHash: string) => request<Approval>(`/approvals/${id}/approve`, json('POST', { parameter_hash: parameterHash })),
    reject: (id: EntityId, parameterHash: string) => request<Approval>(`/approvals/${id}/reject`, json('POST', { parameter_hash: parameterHash })),
  },
  notifications: {
    list: async () => list(await request<NotificationChannel[] | ApiList<NotificationChannel>>('/notifications')),
    create: (input: NotificationChannelInput) => request<NotificationChannel>('/notifications', json('POST', input)),
    test: (id: EntityId) => request<{ ok: boolean }>(`/notifications/${id}/test`, json('POST')),
    remove: (id: EntityId) => request<void>(`/notifications/${id}`, json('DELETE')),
  },
  mcp: {
    tokens: async () => list(await request<McpToken[] | ApiList<McpToken>>('/mcp/tokens')),
    createToken: (input: McpTokenInput) => request<McpToken>('/mcp/tokens', json('POST', input)),
    revokeToken: (id: EntityId) => request<void>(`/mcp/tokens/${id}`, json('DELETE')),
  },
  audit: {
    list: async (query?: { action?: string; outcome?: string; limit?: number }) => list(await request<AuditEvent[] | ApiList<AuditEvent>>('/audit', {}, query)),
  },
  ssh: {
    createSession: (serverId: EntityId) => request<SshSession>(`/servers/${serverId}/ssh-sessions`, json('POST')),
  },
}

interface StreamOptions {
  signal?: AbortSignal
  lastEventId?: string
  onEvent: (event: RunLogEvent) => void
}

export async function streamRunEvents(runId: EntityId, options: StreamOptions): Promise<void> {
  /** 读取 SSE 日志流，按事件边界解析并把旧格式归一化给页面。 */
  const headers = new Headers({ Accept: 'text/event-stream' })
  const token = tokenStorage.get()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.lastEventId) headers.set('Last-Event-ID', options.lastEventId)
  const response = await fetch(buildUrl(`/runs/${runId}/events`), { headers, signal: options.signal })
  if (!response.ok || !response.body) {
    throw new ApiError(response.status, `日志流连接失败（${response.status}）`)
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += value
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() || ''
    for (const block of blocks) {
      let id: string | undefined
      let eventName: string | undefined
      const data: string[] = []
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith(':')) continue
        const separator = line.indexOf(':')
        const field = separator < 0 ? line : line.slice(0, separator)
        const rawValue = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '')
        if (field === 'id') id = rawValue
        if (field === 'event') eventName = rawValue
        if (field === 'data') data.push(rawValue)
      }
      if (!data.length) continue
      const raw = data.join('\n')
      let parsed: RunLogEvent
      try {
        const candidate = JSON.parse(raw) as RunLogEvent | string | {
          id?: number
          sequence?: number
          level?: string
          stage?: string | null
          message?: string
          created_at?: string
          status?: RunLogEvent['status']
        }
        if (typeof candidate === 'string') {
          parsed = { message: candidate }
        } else if (candidate && 'sequence' in candidate) {
          parsed = {
            id: String(candidate.sequence),
            timestamp: candidate.created_at,
            stage: candidate.stage || undefined,
            stream: candidate.level === 'error' ? 'stderr' : candidate.level === 'info' ? 'stdout' : 'system',
            message: candidate.message || '',
          }
        } else if (candidate && 'status' in candidate && !('message' in candidate)) {
          parsed = { message: `流水线状态：${candidate.status}`, status: candidate.status }
        } else {
          parsed = candidate as RunLogEvent
        }
      } catch {
        parsed = { message: raw }
      }
      options.onEvent({ ...parsed, id: parsed.id || id, event: parsed.event || eventName })
    }
  }
}

export function resolveWebSocketUrl(session: SshSession): string {
  /** 将 API 返回的相对或 HTTP 地址转换为当前浏览器可连接的 WebSocket 地址。 */
  const raw = new URL(session.websocket_url, window.location.href)
  if (raw.protocol === 'http:') raw.protocol = 'ws:'
  if (raw.protocol === 'https:') raw.protocol = 'wss:'
  return raw.toString()
}
