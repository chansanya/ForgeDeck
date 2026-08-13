"""使用 Docker Buildx 构建并推送应用镜像，返回可部署的不可变 digest。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devops.runner.credentials import RegistryCredentials, write_docker_config
from devops.runner.process import AsyncCommandRunner, CommandSpec, ProcessOutputSink
from devops.runner.source import resolve_repository_path

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BuildRequest:
    source_root: Path
    dockerfile_path: str
    context_path: str
    image_ref: str
    platforms: tuple[str, ...] = ()
    build_args: Mapping[str, str] | None = None
    labels: Mapping[str, str] | None = None
    registry_credentials: RegistryCredentials | None = None
    pull: bool = True
    no_cache: bool = False
    timeout_seconds: float = 1800

    def __post_init__(self) -> None:
        """在构建请求进入 Runner 前拒绝空镜像引用和无效超时。"""
        if not self.image_ref.strip() or any(char in self.image_ref for char in "\x00\r\n"):
            raise ValueError("image_ref is invalid")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    image_ref: str
    digest: str
    immutable_ref: str
    metadata: Mapping[str, object]


class BuildxBuilder:
    def __init__(self, command_runner: AsyncCommandRunner) -> None:
        """注入参数数组命令执行器，保证 Buildx 不经过 shell。"""
        self._commands = command_runner

    async def build_and_push(
        self,
        request: BuildRequest,
        *,
        sink: ProcessOutputSink | None = None,
        cancel_event: object = None,
    ) -> BuildArtifact:
        """校验源码边界后执行 Buildx 推送，并返回可审计的 immutable digest 引用。"""
        source_root = request.source_root.resolve(strict=True)
        dockerfile = resolve_repository_path(source_root, request.dockerfile_path)
        context = resolve_repository_path(source_root, request.context_path)
        if not dockerfile.is_file():
            raise ValueError("Dockerfile path does not point to a file")
        if not context.is_dir():
            raise ValueError("build context does not point to a directory")

        metadata_fd, metadata_name = tempfile.mkstemp(prefix="devops-buildx-", suffix=".json")
        os.close(metadata_fd)
        metadata_path = Path(metadata_name)
        docker_config = tempfile.TemporaryDirectory(prefix="devops-docker-config-")
        try:
            docker_config_path = Path(docker_config.name)
            if request.registry_credentials is not None:
                await asyncio.to_thread(
                    write_docker_config,
                    docker_config_path,
                    request.registry_credentials,
                    request.image_ref,
                )
            command_environment: Mapping[str, str] = {
                "DOCKER_CONFIG": str(docker_config_path)
            }
            argv = [
                "docker",
                "buildx",
                "build",
                "--file",
                str(dockerfile),
                "--tag",
                request.image_ref,
                "--metadata-file",
                str(metadata_path),
                "--push",
            ]
            if request.pull:
                argv.append("--pull")
            if request.no_cache:
                argv.append("--no-cache")
            if request.platforms:
                argv.extend(("--platform", ",".join(request.platforms)))
            for name, value in sorted((request.build_args or {}).items()):
                _validate_build_key(name, "build argument")
                argv.extend(("--build-arg", f"{name}={value}"))
            for name, value in sorted((request.labels or {}).items()):
                _validate_build_key(name, "label")
                argv.extend(("--label", f"{name}={value}"))
            argv.append(str(context))
            result = await self._commands.run(
                CommandSpec(
                    argv=tuple(argv),
                    timeout=request.timeout_seconds,
                    stage="build",
                    max_capture_bytes=4 * 1024 * 1024,
                    env=command_environment,
                ),
                sink=sink,
                cancel_event=cancel_event,  # type: ignore[arg-type]
            )
            result.check_returncode()
            metadata = _read_metadata(metadata_path)
            digest = _metadata_digest(metadata)
            if digest is None:
                digest = await self.resolve_registry_digest(
                    request.image_ref,
                    sink=sink,
                    cancel_event=cancel_event,
                    environment=command_environment,
                )
            return BuildArtifact(
                image_ref=request.image_ref,
                digest=digest,
                immutable_ref=f"{_repository_without_tag(request.image_ref)}@{digest}",
                metadata=metadata,
            )
        finally:
            await asyncio.to_thread(metadata_path.unlink, missing_ok=True)
            await asyncio.to_thread(docker_config.cleanup)

    async def resolve_registry_digest(
        self,
        image_ref: str,
        *,
        sink: ProcessOutputSink | None = None,
        cancel_event: object = None,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        """在 Buildx 元数据缺失时读取 Registry manifest，生成稳定内容摘要。"""
        result = await self._commands.run(
            CommandSpec(
                argv=("docker", "buildx", "imagetools", "inspect", "--raw", image_ref),
                timeout=120,
                stage="registry",
                max_capture_bytes=16 * 1024 * 1024,
                env=environment,
            ),
            sink=sink,
            cancel_event=cancel_event,  # type: ignore[arg-type]
        )
        result.check_returncode()
        if result.output_truncated:
            raise RuntimeError("registry manifest exceeded the safe capture limit")
        return f"sha256:{hashlib.sha256(result.stdout).hexdigest()}"


def _read_metadata(path: Path) -> Mapping[str, object]:
    """读取 Buildx 元数据并确认顶层结构是 JSON 对象。"""
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Buildx did not produce valid build metadata") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Buildx metadata must be a JSON object")
    return value


def _metadata_digest(metadata: Mapping[str, object]) -> str | None:
    """从 Buildx 可能使用的元数据字段中提取规范 sha256 digest。"""
    candidates: list[object] = [
        metadata.get("containerimage.digest"),
        metadata.get("buildx.build.provenance"),
    ]
    descriptor = metadata.get("containerimage.descriptor")
    if isinstance(descriptor, dict):
        candidates.append(descriptor.get("digest"))
    for candidate in candidates:
        if isinstance(candidate, str) and _DIGEST.fullmatch(candidate):
            return candidate
    return None


def _repository_without_tag(image_ref: str) -> str:
    """移除镜像引用的 tag，保留 Registry、命名空间和仓库部分。"""
    if "@" in image_ref:
        return image_ref.split("@", 1)[0]
    slash = image_ref.rfind("/")
    colon = image_ref.rfind(":")
    return image_ref[:colon] if colon > slash else image_ref


def _validate_build_key(value: str, kind: str) -> None:
    """校验 build-arg/label 名称，避免控制字符改变命令参数边界。"""
    if not value or any(character in value for character in "\x00\r\n="):
        raise ValueError(f"invalid {kind} name")
