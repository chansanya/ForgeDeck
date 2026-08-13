"""定义 API 与 Runner 共用的环境配置及生产环境安全校验。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DEVOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Light DevOps"
    environment: str = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite+aiosqlite:///./data/devops.db"
    auto_create_schema: bool = True
    data_dir: Path = Path("./data")
    log_dir: Path = Path("./logs")
    workspace_dir: Path = Path("./data/workspaces")
    frontend_dir: Path | None = Path("../frontend/dist")
    template_dir: Path = Path("../templates")
    secret_key_path: Path = Path("./secrets/master.key")
    jwt_issuer: str = "light-devops"
    access_token_minutes: int = 30
    admin_username: str = "admin"
    admin_initial_password: str | None = Field(default=None, min_length=12)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    sse_poll_interval_seconds: float = 0.5
    sse_batch_size: int = 200
    runner_lease_seconds: int = 60
    run_log_retention_days: int = 30
    audit_retention_days: int = 180
    mcp_enabled: bool = True
    mcp_token: str | None = None
    mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ]
    )
    mcp_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
    )
    runner_internal_url: str = "http://127.0.0.1:8765"
    internal_token: str | None = None

    @field_validator(
        "cors_origins",
        "mcp_allowed_hosts",
        "mcp_allowed_origins",
        mode="before",
    )
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        """将环境变量中的逗号分隔或 JSON 列表统一转换为配置列表。"""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("access_token_minutes", "runner_lease_seconds")
    @classmethod
    def positive_seconds(cls, value: int) -> int:
        """拒绝零或负数的时长配置，避免租约和令牌立即失效。"""
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator("mcp_token", "internal_token")
    @classmethod
    def validate_service_token(cls, value: str | None) -> str | None:
        """校验内部服务令牌长度，并去掉配置文件中意外的首尾空白。"""
        if value is None:
            return None
        value = value.strip()
        if len(value) < 32:
            raise ValueError("service tokens must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def reject_production_placeholders(self) -> Settings:
        """阻止生产环境使用引导令牌或常见占位密码。"""
        if self.environment.lower() != "production":
            return self
        if self.mcp_token is not None:
            raise ValueError(
                "mcp_token bootstrap access is disabled in production; "
                "issue a short-lived database-backed MCP token instead"
            )
        if not self.internal_token:
            raise ValueError("internal_token is required in production")
        protected = {
            "admin_initial_password": self.admin_initial_password,
            "internal_token": self.internal_token,
        }
        for name, value in protected.items():
            if value is not None and _is_placeholder(value):
                raise ValueError(f"{name} uses an insecure placeholder value")
        return self


@lru_cache
def get_settings() -> Settings:
    """返回进程内缓存的配置对象，避免每次请求重复解析环境变量。"""
    return Settings()


def _is_placeholder(value: str) -> bool:
    """识别常见的默认占位值，供生产配置校验拒绝弱密钥。"""
    normalized = value.strip().lower()
    return normalized in {"changeme", "change-me", "password", "secret"} or normalized.startswith(
        ("replace-", "replace_", "replace ")
    )
