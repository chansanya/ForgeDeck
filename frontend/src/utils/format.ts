/* 提供字节、时间、百分比、主机和 commit 的纯展示格式化函数。 */

export function formatBytes(value?: number | null, precision = 1): string {
  /** 将字节数格式化为适合卡片展示的 IEC 单位。 */
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (value === 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const index = Math.min(Math.floor(Math.log(Math.abs(value)) / Math.log(1024)), units.length - 1)
  return `${(value / 1024 ** index).toFixed(index === 0 ? 0 : precision)} ${units[index]}`
}

export function formatDate(value?: string | null, includeSeconds = false): string {
  /** 将 ISO 时间转换为中文短日期时间。 */
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: includeSeconds ? '2-digit' : undefined,
    hour12: false,
  }).format(date)
}

export function formatFullDate(value?: string | null): string {
  /** 将 ISO 时间转换为包含年份和秒的完整中文时间。 */
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatDuration(seconds?: number | null): string {
  /** 将秒数转换为紧凑的时分秒文本。 */
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`
  const minutes = Math.floor(seconds / 60)
  const remaining = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes}m ${remaining}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

export function shortSha(value?: string | null): string {
  /** 截取 commit 或 digest 的前八位用于列表展示。 */
  return value ? value.slice(0, 8) : '—'
}

export function percent(used?: number | null, total?: number | null): number {
  /** 计算并限制资源使用百分比到 0～100。 */
  if (!used || !total) return 0
  return Math.min(100, Math.max(0, Number(((used / total) * 100).toFixed(1))))
}

export function maskHost(value?: string | null): string {
  /** 遮盖 SSH 用户名后的主机标识，减少页面敏感信息暴露。 */
  if (!value) return '—'
  if (!value.includes('@')) return value
  const [name, domain] = value.split('@')
  return `${name?.slice(0, 2)}***@${domain}`
}

export function durationSeconds(startedAt?: string | null, finishedAt?: string | null): number | null {
  /** 计算运行时长；未结束任务使用当前时间作为终点。 */
  if (!startedAt) return null
  const start = new Date(startedAt).getTime()
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now()
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, (end - start) / 1000) : null
}

export function serverStatus(server: { enabled: boolean; last_seen_at?: string | null }): 'online' | 'offline' | 'unknown' {
  /** 根据启用状态和最近心跳判断服务器在线、离线或未知。 */
  if (!server.enabled) return 'offline'
  if (!server.last_seen_at) return 'unknown'
  return Date.now() - new Date(server.last_seen_at).getTime() < 120_000 ? 'online' : 'offline'
}
