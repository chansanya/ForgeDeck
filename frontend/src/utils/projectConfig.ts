/* 在表单字段与后端项目流水线配置对象之间执行安全转换和校验。 */

export interface PipelineFields {
  serviceName: string
  defaultEnvironmentId: string | null
}

export function readPipelineFields(config: Record<string, unknown>): PipelineFields {
  /** 从高级配置读取表单可见字段，并提供安全默认值。 */
  return {
    serviceName: typeof config.service_name === 'string' && config.service_name.trim()
      ? config.service_name
      : 'app',
    defaultEnvironmentId: typeof config.default_environment_id === 'string' && config.default_environment_id
      ? config.default_environment_id
      : null,
  }
}

export function advancedPipelineConfig(config: Record<string, unknown>): Record<string, unknown> {
  /** 移除表单托管字段，保留用户编辑的高级流水线配置。 */
  const advanced = { ...config }
  delete advanced.service_name
  delete advanced.default_environment_id
  return advanced
}

export function mergePipelineConfig(config: Record<string, unknown>, fields: PipelineFields): Record<string, unknown> {
  /** 将表单字段合并回高级配置，避免覆盖未知后端字段。 */
  const merged = advancedPipelineConfig(config)
  merged.service_name = fields.serviceName.trim() || 'app'
  if (fields.defaultEnvironmentId) merged.default_environment_id = fields.defaultEnvironmentId
  return merged
}

export function validServiceName(value: string): boolean {
  /** 校验 Compose 服务名是否符合后端允许的字符范围。 */
  return /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value.trim())
}

export function parseJsonObject(value: string, label: string): Record<string, unknown> {
  /** 解析并校验文本必须是 JSON 对象，而非数组或标量。 */
  if (!value.trim()) return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(value)
  } catch {
    throw new Error(`${label}不是有效 JSON`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象`)
  }
  return parsed as Record<string, unknown>
}

export function parseStringJsonObject(value: string, label: string): Record<string, string> {
  /** 解析只允许字符串值的 JSON 对象，用于环境变量和构建参数。 */
  const parsed = parseJsonObject(value, label)
  const result: Record<string, string> = {}
  for (const [key, item] of Object.entries(parsed)) {
    if (typeof item !== 'string') throw new Error(`${label}.${key} 必须是字符串`)
    result[key] = item
  }
  return result
}

export function parseKeyValue(value: string): Record<string, string> {
  /** 将多行 KEY=value 文本解析为对象，并拒绝重复或非法变量名。 */
  const result: Record<string, string> = {}
  for (const [index, rawLine] of value.split(/\r?\n/).entries()) {
    const trimmedLine = rawLine.trim()
    if (!trimmedLine || trimmedLine.startsWith('#')) continue
    const separator = rawLine.indexOf('=')
    if (separator <= 0) throw new Error(`环境变量第 ${index + 1} 行必须是 KEY=value`)
    const key = rawLine.slice(0, separator).trim()
    const item = rawLine.slice(separator + 1)
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new Error(`环境变量名 ${key} 无效`)
    if (Object.hasOwn(result, key)) throw new Error(`环境变量 ${key} 重复`)
    result[key] = item
  }
  return result
}

export function validateHealthcheck(value: Record<string, unknown>): void {
  /** 在提交前校验四类健康检查的必填字段和正数时限。 */
  const kind = String(value.kind || value.type || 'compose')
  if (!['compose', 'http', 'tcp', 'command'].includes(kind)) {
    throw new Error('健康检查 kind 仅支持 compose、http、tcp 或 command')
  }
  if (kind === 'http' && typeof value.url !== 'string') throw new Error('HTTP 健康检查必须配置 url')
  if (kind === 'tcp' && (typeof value.host !== 'string' || typeof value.port !== 'number')) {
    throw new Error('TCP 健康检查必须配置 host 和数字 port')
  }
  if (kind === 'command' && (!Array.isArray(value.command) || !value.command.length || !value.command.every((item) => typeof item === 'string'))) {
    throw new Error('命令健康检查的 command 必须是非空字符串数组')
  }
  for (const key of ['timeout_seconds', 'interval_seconds']) {
    if (value[key] !== undefined && (typeof value[key] !== 'number' || value[key] <= 0)) {
      throw new Error(`健康检查 ${key} 必须是正数`)
    }
  }
}

export function isSafeRepositoryPath(value: string): boolean {
  /** 判断仓库相对路径不会以绝对路径或 .. 逃出源码目录。 */
  const normalized = value.replaceAll('\\', '/').trim()
  return Boolean(normalized) && !normalized.startsWith('/') && !normalized.split('/').includes('..')
}

export function formatJson(value: Record<string, unknown> | Record<string, string>): string {
  /** 以稳定缩进格式序列化编辑器中的 JSON 对象。 */
  return JSON.stringify(value || {}, null, 2)
}

export function formatKeyValue(value: Record<string, string>): string {
  /** 按键排序输出 KEY=value，减少配置快照的无意义差异。 */
  return Object.entries(value || {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${key}=${item}`)
    .join('\n')
}
