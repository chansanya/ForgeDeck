/* 定义与后端 Pydantic Schema 对应的控制台传输类型。 */

export type EntityId = string

export type RunStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export type NotificationKind = 'dingtalk' | 'webhook' | 'smtp'

export type McpScope =
  | 'read:status'
  | 'read:logs'
  | 'request:build'
  | 'request:deploy'
  | 'request:rollback'
  | 'request:script'

export interface User {
  id: EntityId
  username: string
  is_active: boolean
  created_at: string
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in?: number
  user?: User
}

export interface Project {
  id: EntityId
  name: string
  repo_url: string
  default_branch: string
  git_credential_id: EntityId | null
  webhook_credential_id: EntityId | null
  registry_credential_id: EntityId | null
  dockerfile_source: 'repository' | 'inline'
  dockerfile_path: string
  dockerfile_content: string | null
  build_context: string
  image_repository: string | null
  build_args: Record<string, string>
  pipeline_config: Record<string, unknown>
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface ProjectInput {
  name: string
  repo_url: string
  default_branch: string
  git_credential_id?: EntityId | null
  webhook_credential_id?: EntityId | null
  registry_credential_id?: EntityId | null
  dockerfile_source: 'repository' | 'inline'
  dockerfile_path: string
  dockerfile_content?: string | null
  build_context: string
  image_repository?: string | null
  build_args: Record<string, string>
  pipeline_config: Record<string, unknown>
  enabled: boolean
}

export interface Environment {
  id: EntityId
  project_id: EntityId
  name: string
  server_id: EntityId
  compose_source: 'repository' | 'inline'
  compose_path: string
  compose_content: string | null
  deploy_path: string
  env_config: Record<string, string>
  healthcheck: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface EnvironmentInput {
  name: string
  server_id: EntityId
  compose_source: 'repository' | 'inline'
  compose_path: string
  compose_content?: string | null
  deploy_path: string
  env_config: Record<string, string>
  healthcheck: Record<string, unknown>
}

export interface ProjectTemplate {
  id: string
  name: string
  language: string
  description: string
  dockerfile: string
  compose: string
}

export interface PipelineRun {
  id: EntityId
  project_id: EntityId
  environment_id?: EntityId | null
  trigger_type: 'manual' | 'gitlab' | 'github' | 'gitee' | 'mcp' | string
  trigger_actor: string | null
  provider: string | null
  delivery_id: string | null
  status: RunStatus
  ref: string
  commit_sha: string
  snapshot_sha256: string
  image_ref: string | null
  current_stage?: string | null
  image_digest?: string | null
  started_at?: string | null
  finished_at?: string | null
  cancel_requested: boolean
  created_at: string
  updated_at: string
  error_message?: string | null
}

export interface RunRequest {
  commit_sha: string
  ref: string
  environment_id?: EntityId
}

export interface RunLogEvent {
  id?: string
  event?: string
  timestamp?: string
  stage?: string
  stream?: 'stdout' | 'stderr' | 'system'
  message: string
  status?: RunStatus
}

export interface Server {
  id: EntityId
  name: string
  host: string
  port: number
  username: string
  ssh_credential_id: EntityId | null
  host_key: string | null
  labels: Record<string, string>
  enabled: boolean
  last_seen_at?: string | null
  created_at: string
  updated_at: string
}

export interface ServerInput {
  name: string
  host: string
  port: number
  username: string
  ssh_credential_id?: EntityId | null
  host_key?: string | null
  labels: Record<string, string>
  enabled: boolean
}

export interface HostKeyScanResult {
  algorithm: string
  fingerprint: string
  public_key: string
}

export interface MetricPoint {
  id: number
  server_id: EntityId
  collected_at: string
  cpu_cores: number
  cpu_percent: number
  memory_used: number
  memory_total: number
  disk_used: number
  disk_total: number
  network_rx: number
  network_tx: number
}

export type DockerContainer = Record<string, unknown> & {
  ID?: string
  Name?: string
  Names?: string
  Image?: string
  State?: string
  Status?: string
  Ports?: string
  Mounts?: string
  Networks?: string
  Labels?: string
  CreatedAt?: string
  Size?: string
}

export type DockerImage = Record<string, unknown> & {
  ID?: string
  Name?: string
  Repository?: string
  Tag?: string
  Digest?: string
  Size?: string
  CreatedAt?: string
  Containers?: string
}

export type DockerVolume = Record<string, unknown> & {
  Name?: string
  Driver?: string
  Mountpoint?: string
  Labels?: string
  Scope?: string
  Size?: string
}

export type DockerNetwork = Record<string, unknown> & {
  ID?: string
  Name?: string
  Driver?: string
  Scope?: string
  IPv6?: string
  Internal?: string
  Labels?: string
  CreatedAt?: string
}

export interface DockerOverview {
  server_id: EntityId
  version: Record<string, unknown>
  disk_usage: Array<Record<string, unknown>>
  containers: DockerContainer[]
  images: DockerImage[]
  volumes: DockerVolume[]
  networks: DockerNetwork[]
}

export type DockerAction =
  | 'container_start'
  | 'container_stop'
  | 'container_restart'
  | 'container_remove'
  | 'image_remove'
  | 'volume_remove'
  | 'network_remove'
  | 'compose_up'
  | 'compose_down'
  | 'compose_restart'

export interface DockerActionInput {
  target: string
  confirmation?: string
  options?: {
    environment_id?: EntityId
    timeout_seconds?: number
  }
}

export interface DockerActionResult {
  ok: boolean
  action: string
  server_id: EntityId
}

export interface Deployment {
  id: EntityId
  project_id: EntityId
  environment_id: EntityId
  server_id: EntityId
  run_id?: EntityId | null
  revision: string
  image_digest: string
  status: 'pending' | 'deploying' | 'healthy' | 'failed' | 'rolled_back'
  image_ref: string
  previous_deployment_id?: EntityId | null
  previous_revision: string | null
  compose_sha256: string | null
  healthcheck_result: Record<string, unknown>
  started_at?: string | null
  finished_at?: string | null
  created_at: string
  updated_at: string
  error_message: string | null
}

export interface Script {
  id: EntityId
  name: string
  description?: string | null
  content?: string
  enabled: boolean
  current_version: number
  sha256: string
  created_at: string
  updated_at: string
}

export interface ScriptInput {
  name: string
  description?: string
  content: string
  enabled: boolean
}

export interface Credential {
  id: EntityId
  name: string
  kind: 'git' | 'ssh' | 'registry' | 'webhook' | 'smtp' | 'notification' | string
  metadata: Record<string, unknown>
  version: number
  has_secret: boolean
  created_at: string
  updated_at: string
}

export interface CredentialInput {
  name: string
  kind: string
  metadata: Record<string, unknown>
  secret: string
}

export interface Approval {
  id: EntityId
  kind: 'build' | 'deploy' | 'rollback' | 'script' | string
  state: 'pending' | 'approved' | 'rejected' | 'expired' | 'executing' | 'succeeded' | 'failed'
  requested_by: string
  approved_by: string | null
  parameters: Record<string, unknown>
  parameter_hash: string
  preview: Record<string, unknown>
  result: Record<string, unknown>
  created_at: string
  updated_at: string
  decided_at?: string | null
  expires_at?: string | null
}

export interface NotificationChannel {
  id: EntityId
  name: string
  kind: NotificationKind
  enabled: boolean
  events: string[]
  target_hint: string | null
  last_tested_at: string | null
  updated_at: string
  created_at: string
}

export interface NotificationChannelInput {
  name: string
  kind: NotificationKind
  enabled: boolean
  events: string[]
  config: Record<string, unknown>
}

export interface McpToken {
  id: EntityId
  name: string
  scopes: McpScope[]
  expires_at: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
  token: string | null
}

export interface McpTokenInput {
  name: string
  scopes: McpScope[]
  expires_in_seconds: number
}

export interface AuditEvent {
  id: EntityId
  actor: string
  action: string
  resource_type: string
  resource_id?: string | null
  outcome: 'success' | 'failure' | 'denied' | string
  source_ip?: string | null
  trace_id: string | null
  details: Record<string, unknown>
  created_at: string
}

export interface DashboardSummary {
  server_count: number
  project_count: number
  queued_runs: number
  running_runs: number
  failed_runs: number
  pending_approvals: number
}

export interface SshSession {
  id: EntityId
  websocket_url: string
  expires_in: number
}

export interface ApiList<T> {
  items: T[]
  total?: number
}
