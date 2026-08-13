"""解析 Runner 所需凭据并生成可回收的临时 Git 与 Registry 配置。"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devops.runner.ssh import SSHCredentials


@dataclass(frozen=True, slots=True)
class GitCredentials:
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class RegistryCredentials:
    username: str
    password: str
    endpoint: str | None = None


def parse_ssh_credentials(secret: str) -> SSHCredentials:
    """兼容 JSON、私钥和密码三种存储格式，并拒绝缺少认证材料的 JSON。"""
    parsed = _parse_secret_object(secret)
    if parsed is not None:
        password = _optional_string(parsed, "password")
        private_key = _optional_string(parsed, "private_key", "privateKey", "key")
        passphrase = _optional_string(parsed, "passphrase", "key_passphrase")
        if password is None and private_key is None:
            raise ValueError("SSH credential JSON requires password or private_key")
        return SSHCredentials(
            password=password,
            private_key=private_key,
            passphrase=passphrase,
        )
    if "PRIVATE KEY-----" in secret:
        return SSHCredentials(private_key=secret)
    return SSHCredentials(password=secret)


def parse_git_credentials(secret: str) -> GitCredentials:
    """解析 Git 用户名与令牌，纯字符串令牌默认使用 git 用户名。"""
    parsed = _parse_secret_object(secret)
    if parsed is None:
        return GitCredentials(username="git", password=secret)
    username = _optional_string(parsed, "username", "user") or "git"
    password = _optional_string(parsed, "password", "token", "access_token")
    if password is None:
        raise ValueError("Git credential JSON requires password or token")
    return GitCredentials(username=username, password=password)


def parse_registry_credentials(
    secret: str,
    details: Mapping[str, object] | None = None,
) -> RegistryCredentials:
    """解析 Registry 登录信息并校验用户名、密码和端点不含控制字符。"""
    metadata = details or {}
    parsed = _parse_secret_object(secret)
    if parsed is None:
        username = _optional_string(metadata, "username", "user")
        if username is None:
            raise ValueError("plain registry credentials require metadata.username")
        password = secret
        endpoint = _optional_string(metadata, "endpoint", "registry")
    else:
        username = _optional_string(parsed, "username", "user") or _optional_string(
            metadata, "username", "user"
        )
        password = _optional_string(parsed, "password", "token", "access_token")
        endpoint = _optional_string(parsed, "endpoint", "registry") or _optional_string(
            metadata, "endpoint", "registry"
        )
        if username is None or password is None:
            raise ValueError("registry credential JSON requires username and password or token")
    if not username or any(character in username for character in "\x00\r\n"):
        raise ValueError("registry username is invalid")
    if not password or "\x00" in password:
        raise ValueError("registry password is invalid")
    if endpoint is not None:
        endpoint = endpoint.strip().rstrip("/")
        if not endpoint or any(
            character.isspace() or ord(character) < 32 for character in endpoint
        ):
            raise ValueError("registry endpoint is invalid")
    return RegistryCredentials(username=username, password=password, endpoint=endpoint)


def docker_config_bytes(credentials: RegistryCredentials, image_ref: str) -> bytes:
    """生成仅包含目标 Registry 的临时 Docker auth 配置，调用方负责销毁文件。"""
    endpoint = credentials.endpoint or _registry_from_image(image_ref)
    token = base64.b64encode(
        f"{credentials.username}:{credentials.password}".encode()
    ).decode("ascii")
    return json.dumps(
        {"auths": {endpoint: {"auth": token}}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def write_docker_config(
    directory: Path,
    credentials: RegistryCredentials,
    image_ref: str,
) -> Path:
    """以 0700/0600 权限写出临时 Docker 配置，降低凭据被本机其他用户读取的风险。"""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / "config.json"
    path.write_bytes(docker_config_bytes(credentials, image_ref))
    os.chmod(path, 0o600)
    return path


def create_git_askpass(directory: Path, credentials: GitCredentials) -> tuple[Path, dict[str, str]]:
    """创建一次性 Git askpass 脚本，并通过环境变量注入凭据而非拼接 URL。"""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "git-askpass.sh"
    script.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *[Uu]sername*) printf '%s\\n' \"$DEVOPS_GIT_USERNAME\" ;;\n"
        "  *) printf '%s\\n' \"$DEVOPS_GIT_PASSWORD\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o700)
    return script, {
        "GIT_ASKPASS": str(script),
        "GIT_ASKPASS_REQUIRE": "force",
        "DEVOPS_GIT_USERNAME": credentials.username,
        "DEVOPS_GIT_PASSWORD": credentials.password,
    }


def _parse_secret_object(secret: str) -> dict[str, object] | None:
    """将看似 JSON 的凭据解析为对象，普通纯文本凭据返回 None。"""
    stripped = secret.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError("credential secret contains invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("credential secret JSON must be an object")
    return parsed


def _optional_string(value: Mapping[str, object], *keys: str) -> str | None:
    """按候选字段读取字符串值，并拒绝类型不匹配的敏感配置。"""
    for key in keys:
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str):
                raise ValueError(f"credential field {key!r} must be a string")
            return item
    return None


def _registry_from_image(image_ref: str) -> str:
    """从镜像引用推导 Registry 主机，未显式指定时使用 Docker Hub。"""
    reference = image_ref.split("@", 1)[0]
    first = reference.split("/", 1)[0]
    if "/" not in reference or not (
        "." in first or ":" in first or first == "localhost"
    ):
        return "https://index.docker.io/v1"
    return first
