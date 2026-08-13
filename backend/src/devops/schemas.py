"""定义 REST API 的 Pydantic 请求、响应模型与输入安全校验。

路径、仓库 URL、健康检查和敏感字段的边界在进入业务层前统一收紧。
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from devops.domain.models import (
    ApprovalState,
    CredentialKind,
    DeploymentStatus,
    NotificationKind,
    OperationKind,
    RunStatus,
)

_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SENSITIVE_METADATA_KEYS = {
    "access_token",
    "api_token",
    "client_secret",
    "key_passphrase",
    "passphrase",
    "password",
    "private_key",
    "secret",
    "token",
}


def _validate_credential_metadata(value: dict[str, Any] | None) -> dict[str, Any] | None:
    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
                normalized = re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")
                if normalized in _SENSITIVE_METADATA_KEYS or any(
                    marker in normalized
                    for marker in (
                        "password",
                        "passwd",
                        "passphrase",
                        "private_key",
                        "privatekey",
                        "secret",
                        "token",
                    )
                ):
                    raise ValueError(
                        f"metadata key {key!r} may expose a secret; use the secret field"
                    )
                walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)

    if value is not None:
        walk(value)
    return value


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserRead(ORMModel):
    id: str
    username: str
    is_active: bool
    created_at: datetime


class PasswordChange(BaseModel):
    current_password: SecretStr
    new_password: SecretStr = Field(min_length=12, max_length=256)


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: CredentialKind
    secret: SecretStr = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _metadata = field_validator("metadata")(_validate_credential_metadata)


class CredentialUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    secret: SecretStr | None = Field(default=None, min_length=1)
    metadata: dict[str, Any] | None = None

    _metadata = field_validator("metadata")(_validate_credential_metadata)


class CredentialRead(ORMModel):
    id: str
    name: str
    kind: CredentialKind
    metadata: dict[str, Any] = Field(validation_alias="details")
    version: int
    has_secret: bool = True
    created_at: datetime
    updated_at: datetime


class NotificationChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: NotificationKind
    enabled: bool = True
    events: list[str] = Field(default_factory=list, max_length=50)
    config: dict[str, Any]


class NotificationChannelRead(ORMModel):
    id: str
    name: str
    kind: NotificationKind
    enabled: bool
    events: list[str]
    target_hint: str | None
    last_tested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MCPTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[
        Literal[
            "read",
            "request",
            "read:status",
            "read:logs",
            "request:build",
            "request:deploy",
            "request:rollback",
            "request:script",
        ]
    ] = Field(default_factory=lambda: ["read:status"], min_length=1, max_length=8)
    expires_in_seconds: int = Field(default=86400, ge=300, le=2_592_000)


class MCPTokenRead(ORMModel):
    id: str
    name: str
    scopes: list[str]
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    token: str | None = None


def validate_repo_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must stay inside the repository")
    return normalized


def validate_repo_url(value: str | None) -> str:
    if value is None:
        raise ValueError("repo_url cannot be null")
    normalized = value.strip()
    if normalized != value or any(character in normalized for character in "\x00\r\n"):
        raise ValueError("repo_url is invalid")
    if normalized.lower().startswith("ext::"):
        raise ValueError("Git remote helpers are not allowed")
    parsed = urlsplit(normalized)
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("repo_url must use http or https in v1")
        if not parsed.hostname or parsed.password is not None:
            raise ValueError("repo_url host is missing or embeds a password")
        if parsed.username is not None:
            raise ValueError("HTTP repository credentials must use a Credential")
        if parsed.query or parsed.fragment:
            raise ValueError("repo_url cannot contain a query or fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("repo_url contains an invalid port") from exc
        return normalized
    raise ValueError("repo_url must be an absolute HTTP(S) Git URL")


def validate_required_repo_path_update(value: str | None) -> str:
    if value is None:
        raise ValueError("repository path cannot be null")
    return validate_repo_path(value)


def validate_pipeline_config(value: dict[str, Any]) -> dict[str, Any]:
    service_name = value.get("service_name")
    if service_name is not None and (
        not isinstance(service_name, str) or not _SERVICE_NAME.fullmatch(service_name)
    ):
        raise ValueError("pipeline_config.service_name is invalid")
    min_free_bytes = value.get("min_free_bytes")
    if min_free_bytes is not None and (
        isinstance(min_free_bytes, bool)
        or not isinstance(min_free_bytes, int)
        or min_free_bytes < 0
    ):
        raise ValueError("pipeline_config.min_free_bytes must be a non-negative integer")
    default_environment_id = value.get("default_environment_id")
    if default_environment_id is not None and (
        not isinstance(default_environment_id, str) or not default_environment_id
    ):
        raise ValueError("pipeline_config.default_environment_id must be a non-empty string")
    return value


def _validate_nonsecret_mapping(
    value: dict[str, str] | None, field_name: str
) -> dict[str, str] | None:
    if value is None:
        return None
    for name in value:
        separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip())
        normalized = re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")
        if any(
            marker in normalized
            for marker in (
                "password",
                "passwd",
                "token",
                "secret",
                "passphrase",
                "private_key",
                "privatekey",
                "api_key",
                "apikey",
                "access_key",
                "accesskey",
                "auth_key",
                "authkey",
                "credential",
            )
        ):
            raise ValueError(
                f"{field_name} key {name!r} looks secret-bearing; plaintext secrets are not allowed"
            )
    return value


def validate_plain_environment(value: dict[str, str] | None) -> dict[str, str] | None:
    return _validate_nonsecret_mapping(value, "env_config")


def validate_build_args(value: dict[str, str] | None) -> dict[str, str] | None:
    return _validate_nonsecret_mapping(value, "build_args")


def validate_deploy_path(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        "\x00" in value
        or not value.startswith("/")
        or value == "/"
        or ".." in PurePosixPath(value).parts
        or posixpath.normpath(value) != value
    ):
        raise ValueError("deploy_path must be a canonical absolute non-root Linux path")
    return value


def validate_ssh_host(value: str | None) -> str | None:
    if value is None:
        return None
    if value != value.strip() or not value or not value.isascii():
        raise ValueError("host must be a non-empty ASCII hostname or IP address")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("host cannot contain whitespace or control characters")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if len(value) > 253 or any(not _HOST_LABEL.fullmatch(label) for label in value.split(".")):
        raise ValueError("host must be a valid hostname or IP address")
    return value


def validate_host_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or any(character in normalized for character in "\x00\r\n"):
        raise ValueError("host_key must be a fingerprint or OpenSSH public key")
    return normalized


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    repo_url: str = Field(min_length=1, max_length=1000)
    default_branch: str = Field(default="main", min_length=1, max_length=255)
    enabled: bool = True
    git_credential_id: str | None = None
    webhook_credential_id: str | None = None
    registry_credential_id: str | None = None
    dockerfile_source: Literal["repository", "inline"] = "repository"
    dockerfile_path: str = "Dockerfile"
    dockerfile_content: str | None = None
    build_context: str = "."
    image_repository: str | None = Field(default=None, max_length=1000)
    build_args: dict[str, str] = Field(default_factory=dict)
    pipeline_config: dict[str, Any] = Field(default_factory=dict)

    _dockerfile_path = field_validator("dockerfile_path")(validate_repo_path)
    _build_context = field_validator("build_context")(validate_repo_path)
    _repo_url = field_validator("repo_url")(validate_repo_url)
    _build_args = field_validator("build_args")(validate_build_args)
    _pipeline_config = field_validator("pipeline_config")(validate_pipeline_config)

    @model_validator(mode="after")
    def validate_inline_dockerfile(self) -> ProjectCreate:
        if self.dockerfile_source == "inline" and not self.dockerfile_content:
            raise ValueError("dockerfile_content is required for inline source")
        if self.enabled and not self.image_repository:
            raise ValueError("image_repository is required for an enabled project")
        if self.pipeline_config.get("default_environment_id") is not None:
            raise ValueError("a new project cannot reference an environment which does not exist")
        return self


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    repo_url: str | None = Field(default=None, min_length=1, max_length=1000)
    default_branch: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    git_credential_id: str | None = None
    webhook_credential_id: str | None = None
    registry_credential_id: str | None = None
    dockerfile_source: Literal["repository", "inline"] | None = None
    dockerfile_path: str | None = None
    dockerfile_content: str | None = None
    build_context: str | None = None
    image_repository: str | None = Field(default=None, max_length=1000)
    build_args: dict[str, str] | None = None
    pipeline_config: dict[str, Any] | None = None

    _dockerfile_path = field_validator("dockerfile_path")(validate_required_repo_path_update)
    _build_context = field_validator("build_context")(validate_required_repo_path_update)
    _repo_url = field_validator("repo_url")(validate_repo_url)
    _build_args = field_validator("build_args")(validate_build_args)

    @field_validator("pipeline_config")
    @classmethod
    def validate_optional_pipeline_config(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any]:
        if value is None:
            raise ValueError("pipeline_config cannot be null")
        return validate_pipeline_config(value)


class ProjectRead(ORMModel):
    id: str
    name: str
    repo_url: str
    default_branch: str
    enabled: bool
    git_credential_id: str | None
    webhook_credential_id: str | None
    registry_credential_id: str | None
    dockerfile_source: str
    dockerfile_path: str
    dockerfile_content: str | None
    build_context: str
    image_repository: str | None
    build_args: dict[str, Any]
    pipeline_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=100)
    ssh_credential_id: str | None = None
    host_key: str | None = Field(default=None, max_length=16384)
    labels: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    _host = field_validator("host")(validate_ssh_host)
    _host_key = field_validator("host_key")(validate_host_key)

    @model_validator(mode="after")
    def require_host_key_when_enabled(self) -> ServerCreate:
        if self.enabled and not self.host_key:
            raise ValueError("host_key is required for an enabled server")
        return self


class ServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=100)
    ssh_credential_id: str | None = None
    host_key: str | None = Field(default=None, max_length=16384)
    labels: dict[str, str] | None = None
    enabled: bool | None = None

    _host = field_validator("host")(validate_ssh_host)
    _host_key = field_validator("host_key")(validate_host_key)


class ServerRead(ORMModel):
    id: str
    name: str
    host: str
    port: int
    username: str
    ssh_credential_id: str | None
    host_key: str | None
    labels: dict[str, Any]
    enabled: bool
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime


class HostKeyScanRequest(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)

    _host = field_validator("host")(validate_ssh_host)


class HostKeyScanRead(BaseModel):
    algorithm: str
    fingerprint: str
    public_key: str


class TemplateRead(BaseModel):
    id: str
    name: str
    language: str
    description: str | None = None
    dockerfile: str
    compose: str
    default_port: int | None = None
    health_path: str | None = None


class HealthCheckConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["compose", "http", "tcp", "command"] = "compose"
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    interval_seconds: float = Field(default=2, gt=0, le=300)
    url: str | None = Field(default=None, max_length=2000)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    command: list[str] = Field(default_factory=list, max_length=64)
    status_min: int = Field(default=200, ge=100, le=599)
    status_max: int = Field(default=399, ge=100, le=599)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 4096 or "\x00" in item for item in value):
            raise ValueError("healthcheck.command must contain non-empty NUL-free arguments")
        return value

    @model_validator(mode="after")
    def validate_kind_fields(self) -> HealthCheckConfig:
        if self.status_min > self.status_max:
            raise ValueError("healthcheck.status_min cannot exceed status_max")
        if self.kind == "http":
            if not self.url:
                raise ValueError("HTTP health checks require url")
            try:
                parsed = urlsplit(self.url)
            except ValueError as exc:
                raise ValueError("healthcheck.url is invalid") from exc
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError(
                    "healthcheck.url must be an HTTP(S) URL without credentials or fragments"
                )
        elif self.url is not None:
            raise ValueError("healthcheck.url is only valid for HTTP checks")

        if self.kind == "tcp":
            if not self.host or self.port is None:
                raise ValueError("TCP health checks require host and port")
            if (
                not self.host.isascii()
                or self.host.startswith("-")
                or any(
                    character.isspace() or ord(character) < 32
                    for character in self.host
                )
            ):
                raise ValueError("healthcheck.host is invalid")
        elif self.host is not None or self.port is not None:
            raise ValueError("healthcheck.host and port are only valid for TCP checks")

        if self.kind == "command":
            if not self.command:
                raise ValueError("command health checks require an argument array")
        elif self.command:
            raise ValueError("healthcheck.command is only valid for command checks")
        return self


class EnvironmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    server_id: str
    compose_source: Literal["repository", "inline"] = "repository"
    compose_path: str = "compose.yaml"
    compose_content: str | None = None
    deploy_path: str = Field(min_length=1, max_length=1000)
    env_config: dict[str, str] = Field(default_factory=dict)
    healthcheck: HealthCheckConfig = Field(default_factory=HealthCheckConfig)

    _compose_path = field_validator("compose_path")(validate_repo_path)
    _env_config = field_validator("env_config")(validate_plain_environment)
    _deploy_path = field_validator("deploy_path")(validate_deploy_path)

    @model_validator(mode="after")
    def validate_inline_compose(self) -> EnvironmentCreate:
        if self.compose_source == "inline" and not self.compose_content:
            raise ValueError("compose_content is required for inline source")
        return self


class EnvironmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    server_id: str | None = None
    compose_source: Literal["repository", "inline"] | None = None
    compose_path: str | None = None
    compose_content: str | None = None
    deploy_path: str | None = Field(default=None, min_length=1, max_length=1000)
    env_config: dict[str, str] | None = None
    healthcheck: HealthCheckConfig | None = None

    _compose_path = field_validator("compose_path")(validate_required_repo_path_update)
    _env_config = field_validator("env_config")(validate_plain_environment)
    _deploy_path = field_validator("deploy_path")(validate_deploy_path)

    @field_validator("healthcheck")
    @classmethod
    def reject_null_healthcheck(
        cls, value: HealthCheckConfig | None
    ) -> HealthCheckConfig:
        if value is None:
            raise ValueError("healthcheck cannot be null")
        return value


class EnvironmentRead(ORMModel):
    id: str
    project_id: str
    server_id: str
    name: str
    compose_source: str
    compose_path: str
    compose_content: str | None
    deploy_path: str
    env_config: dict[str, Any]
    healthcheck: HealthCheckConfig
    created_at: datetime
    updated_at: datetime


class PipelineTrigger(BaseModel):
    commit_sha: str = Field(pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
    ref: str = Field(min_length=1, max_length=500)
    environment_id: str | None = None


class PipelineRunRead(ORMModel):
    id: str
    project_id: str
    environment_id: str | None
    status: RunStatus
    trigger_type: str
    trigger_actor: str | None
    provider: str | None
    delivery_id: str | None
    commit_sha: str
    ref: str
    snapshot_sha256: str
    image_ref: str | None
    image_digest: str | None
    current_stage: str | None
    cancel_requested: bool
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class RunLogRead(ORMModel):
    id: int
    run_id: str
    sequence: int
    level: str
    stage: str | None
    message: str
    created_at: datetime


class DeploymentRead(ORMModel):
    id: str
    project_id: str
    environment_id: str
    server_id: str
    run_id: str | None
    status: DeploymentStatus
    image_ref: str
    image_digest: str
    revision: str
    previous_deployment_id: str | None
    previous_revision: str | None
    compose_sha256: str | None
    healthcheck_result: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class DeploymentRequestCreate(BaseModel):
    environment_id: str
    image_ref: str = Field(min_length=1, max_length=1000)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")
    revision: str = Field(min_length=1, max_length=255)
    compose_content: str | None = Field(
        default=None,
        description="Required immutable Compose snapshot for repository-sourced environments",
    )


class ScriptCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    content: str = Field(min_length=1)
    enabled: bool = True


class ScriptUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    content: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class ScriptRead(ORMModel):
    id: str
    name: str
    description: str | None
    enabled: bool
    current_version: int
    content: str
    sha256: str
    created_at: datetime
    updated_at: datetime


class ScriptExecutionCreate(BaseModel):
    server_id: str
    arguments: dict[str, str] = Field(default_factory=dict)


class OperationRequestRead(ORMModel):
    id: str
    kind: OperationKind
    state: ApprovalState
    requested_by: str
    approved_by: str | None
    parameters: dict[str, Any]
    parameter_hash: str
    preview: dict[str, Any]
    expires_at: datetime
    decided_at: datetime | None
    result: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ApprovalDecision(BaseModel):
    parameter_hash: str = Field(min_length=64, max_length=64)


class HostMetricRead(ORMModel):
    id: int
    server_id: str
    cpu_cores: int
    cpu_percent: float
    memory_total: int
    memory_used: int
    disk_total: int
    disk_used: int
    network_rx: int
    network_tx: int
    collected_at: datetime


class AuditEventRead(ORMModel):
    id: str
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    source_ip: str | None
    trace_id: str | None
    details: dict[str, Any]
    created_at: datetime


class DashboardSummary(BaseModel):
    server_count: int
    project_count: int
    queued_runs: int
    running_runs: int
    failed_runs: int
    pending_approvals: int
