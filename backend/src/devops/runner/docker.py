"""提供本机与远端 Docker/Compose 类型化管理及危险操作影响预览。"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from devops.runner.ssh import SSHSession

try:
    import docker
except ImportError:  # pragma: no cover - API-only installations intentionally omit it
    docker = None  # type: ignore[assignment]


_OBJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class DockerUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MountSummary:
    type: str
    source: str
    destination: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class ContainerSummary:
    id: str
    name: str
    image: str
    status: str
    state: str
    created_at: str | None
    mounts: tuple[MountSummary, ...]
    labels: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ImageSummary:
    id: str
    tags: tuple[str, ...]
    size: int
    created_at: str | None
    labels: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class VolumeSummary:
    name: str
    driver: str
    mountpoint: str
    labels: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class NetworkSummary:
    id: str
    name: str
    driver: str
    scope: str
    containers: int


@dataclass(frozen=True, slots=True)
class VolumeRemovalImpact:
    name: str
    attached_containers: tuple[str, ...]

    @property
    def safe_to_remove(self) -> bool:
        """仅当没有容器挂载该卷时才允许继续删除。"""
        return not self.attached_containers


class DockerSDKExecutor:
    """Runs the synchronous Docker SDK in a small dedicated thread pool."""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        """初始化受限 Docker SDK 线程池，避免同步 SDK 阻塞事件循环。"""
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if client_factory is None:
            if docker is None:
                raise DockerUnavailableError(
                    "Docker SDK is not installed; install the project with the 'runner' extra"
                )
            client_factory = docker.from_env
        self._client_factory = client_factory
        self._client: Any | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="docker-sdk"
        )
        self._semaphore = asyncio.Semaphore(max_workers)
        self._closed = False

    async def ping(self) -> bool:
        """探测本机 Docker Engine 是否可用。"""
        return bool(await self._call(lambda client: client.ping()))

    async def list_containers(
        self, *, include_stopped: bool = True
    ) -> tuple[ContainerSummary, ...]:
        """列出容器并转换为不暴露 SDK 对象的只读摘要。"""
        containers = await self._call(
            lambda client: client.containers.list(all=include_stopped, sparse=False)
        )
        return tuple(_container_summary(container) for container in containers)

    async def list_images(self) -> tuple[ImageSummary, ...]:
        """列出本机镜像摘要。"""
        images = await self._call(lambda client: client.images.list(all=True))
        return tuple(_image_summary(image) for image in images)

    async def list_volumes(self) -> tuple[VolumeSummary, ...]:
        """列出本机卷摘要。"""
        volumes = await self._call(lambda client: client.volumes.list())
        return tuple(_volume_summary(volume) for volume in volumes)

    async def list_networks(self) -> tuple[NetworkSummary, ...]:
        """列出本机网络及连接数摘要。"""
        networks = await self._call(lambda client: client.networks.list())
        return tuple(_network_summary(network) for network in networks)

    async def disk_usage(self) -> Mapping[str, Any]:
        """读取 Docker Engine 空间统计。"""
        return await self._call(lambda client: client.df())

    async def preview_volume_removal(self, name: str) -> VolumeRemovalImpact:
        """计算卷的挂载容器影响，供危险删除前展示而不执行删除。"""
        _validate_object_name(name, "volume")
        containers = await self._call(
            lambda client: client.containers.list(all=True, filters={"volume": name})
        )
        return VolumeRemovalImpact(name, tuple(sorted(container.name for container in containers)))

    async def remove_volume(self, name: str, *, confirmation: str) -> None:
        """要求精确名称确认且卷无挂载后才执行删除。"""
        _validate_object_name(name, "volume")
        if confirmation != name:
            raise ValueError("volume removal confirmation must exactly match the volume name")
        impact = await self.preview_volume_removal(name)
        if not impact.safe_to_remove:
            attached = ", ".join(impact.attached_containers)
            raise RuntimeError(f"volume {name!r} is attached to containers: {attached}")
        await self._call(lambda client: client.volumes.get(name).remove(force=False))

    async def _call(self, operation: Callable[[Any], Any]) -> Any:
        """在线程池执行同步 SDK 调用，并用信号量限制并发。"""
        if self._closed:
            raise RuntimeError("DockerSDKExecutor is closed")
        async with self._semaphore:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, self._call_sync, operation)

    def _call_sync(self, operation: Callable[[Any], Any]) -> Any:
        """在线程池线程内惰性创建并复用 Docker 客户端。"""
        if self._client is None:
            self._client = self._client_factory()
        return operation(self._client)

    async def aclose(self) -> None:
        """关闭客户端和线程池，防止应用退出时遗留工作线程。"""
        if self._closed:
            return
        self._closed = True
        client = self._client
        if client is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, client.close)
        self._executor.shutdown(wait=True, cancel_futures=True)


class RemoteDockerClient:
    """Typed Docker/Compose operations over an already verified SSH session."""

    def __init__(self, session: SSHSession, *, docker_config: str | None = None) -> None:
        """绑定已完成主机指纹校验的 SSH 会话，并校验远端 Docker 配置目录。"""
        self._session = session
        if docker_config is not None:
            normalized = posixpath.normpath(docker_config)
            if (
                not docker_config.startswith("/")
                or normalized == "/"
                or any(character in docker_config for character in "\x00\r\n")
            ):
                raise ValueError("Docker config directory must be an absolute non-root path")
            docker_config = normalized
        self._docker_config = docker_config

    async def version(self) -> Mapping[str, Any]:
        """读取远端 Docker Engine 服务端版本。"""
        result = await self._session.run(
            ("docker", "version", "--format", "{{json .Server}}"), check=True
        )
        return _load_json_object(result.stdout)

    async def disk_usage(self) -> tuple[Mapping[str, Any], ...]:
        """读取远端 Docker system df 的结构化结果。"""
        result = await self._session.run(
            ("docker", "system", "df", "--format", "{{json .}}"), check=True
        )
        return tuple(_load_json_lines(result.stdout))

    async def compose_version(self) -> str:
        """读取并校验远端 Compose V2 版本字符串。"""
        result = await self._session.run(
            ("docker", "compose", "version", "--short"), check=True
        )
        version = result.stdout.decode(errors="replace").strip()
        if not version:
            raise ValueError("docker compose returned an empty version")
        return version

    async def list_containers(self) -> tuple[Mapping[str, Any], ...]:
        """通过参数数组列出远端全部容器。"""
        result = await self._session.run(
            (
                "docker",
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            check=True,
        )
        return tuple(_load_json_lines(result.stdout))

    async def list_images(self) -> tuple[Mapping[str, Any], ...]:
        """通过参数数组列出远端镜像。"""
        result = await self._session.run(
            (
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            check=True,
        )
        return tuple(_load_json_lines(result.stdout))

    async def list_volumes(self) -> tuple[Mapping[str, Any], ...]:
        """通过参数数组列出远端卷。"""
        result = await self._session.run(
            ("docker", "volume", "ls", "--format", "{{json .}}"), check=True
        )
        return tuple(_load_json_lines(result.stdout))

    async def list_networks(self) -> tuple[Mapping[str, Any], ...]:
        """通过参数数组列出远端网络。"""
        result = await self._session.run(
            (
                "docker",
                "network",
                "ls",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            check=True,
        )
        return tuple(_load_json_lines(result.stdout))

    async def inspect_container(self, name: str) -> Mapping[str, Any]:
        """读取单个容器详情，要求结果严格为一个 JSON 对象。"""
        _validate_object_name(name, "container")
        result = await self._session.run(
            ("docker", "container", "inspect", name), check=True
        )
        value = json.loads(result.stdout)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise ValueError("unexpected docker inspect response")
        return value[0]

    async def container_action(
        self, name: str, action: str, *, timeout_seconds: int = 30
    ) -> None:
        """执行受白名单限制的容器启停操作，拒绝任意 Docker 子命令。"""
        _validate_object_name(name, "container")
        if action not in {"start", "stop", "restart"}:
            raise ValueError("unsupported container action")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("container action timeout is out of range")
        argv = ["docker", "container", action]
        if action in {"stop", "restart"}:
            argv.extend(("--timeout", str(timeout_seconds)))
        argv.append(name)
        await self._session.run(
            argv, timeout_seconds=timeout_seconds + 15, check=True
        )

    async def remove_container(self, name: str, *, confirmation: str) -> None:
        """仅在精确确认且容器已停止时删除容器。"""
        _validate_object_name(name, "container")
        if confirmation != name:
            raise ValueError("container removal confirmation must exactly match the name")
        details = await self.inspect_container(name)
        state = details.get("State") or {}
        if isinstance(state, Mapping) and bool(state.get("Running")):
            raise RuntimeError(f"container {name!r} is running; stop it before removal")
        await self._session.run(("docker", "container", "rm", name), check=True)

    async def preview_volume_removal(self, name: str) -> VolumeRemovalImpact:
        """查询远端卷关联容器，为删除操作生成影响预览。"""
        _validate_object_name(name, "volume")
        result = await self._session.run(
            (
                "docker",
                "container",
                "ls",
                "--all",
                "--filter",
                f"volume={name}",
                "--format",
                "{{.Names}}",
            ),
            check=True,
        )
        attached = tuple(
            sorted(line.decode().strip() for line in result.stdout.splitlines() if line)
        )
        return VolumeRemovalImpact(name=name, attached_containers=attached)

    async def remove_volume(self, name: str, *, confirmation: str) -> None:
        """确认卷无关联容器后删除远端卷。"""
        _validate_object_name(name, "volume")
        if confirmation != name:
            raise ValueError("volume removal confirmation must exactly match the volume name")
        impact = await self.preview_volume_removal(name)
        if not impact.safe_to_remove:
            attached = ", ".join(impact.attached_containers)
            raise RuntimeError(f"volume {name!r} is attached to containers: {attached}")
        await self._session.run(("docker", "volume", "rm", name), check=True)

    async def remove_image(self, image: str, *, confirmation: str) -> None:
        """确认镜像未被容器引用后删除，避免破坏现有部署。"""
        if not image or any(character in image for character in "\x00\r\n"):
            raise ValueError("invalid image reference")
        if confirmation != image:
            raise ValueError("image removal confirmation must exactly match the image reference")
        users = await self._session.run(
            (
                "docker",
                "container",
                "ls",
                "--all",
                "--filter",
                f"ancestor={image}",
                "--format",
                "{{.Names}}",
            ),
            check=True,
        )
        containers = tuple(line.decode().strip() for line in users.stdout.splitlines() if line)
        if containers:
            raise RuntimeError(
                f"image {image!r} is used by containers: {', '.join(sorted(containers))}"
            )
        await self._session.run(("docker", "image", "rm", image), check=True)

    async def remove_network(self, name: str, *, confirmation: str) -> None:
        """确认网络没有连接容器后删除远端网络。"""
        _validate_object_name(name, "network")
        if confirmation != name:
            raise ValueError("network removal confirmation must exactly match the name")
        result = await self._session.run(("docker", "network", "inspect", name), check=True)
        value = json.loads(result.stdout)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise ValueError("unexpected docker network inspect response")
        containers = value[0].get("Containers") or {}
        if containers:
            names = sorted(
                str(item.get("Name") or identifier)
                if isinstance(item, Mapping)
                else str(identifier)
                for identifier, item in containers.items()
            )
            raise RuntimeError(f"network {name!r} is connected to containers: {', '.join(names)}")
        await self._session.run(("docker", "network", "rm", name), check=True)

    async def compose_up(
        self,
        *,
        project_name: str,
        project_directory: str,
        files: Sequence[str],
        env_file: str | None = None,
        wait: bool = True,
        wait_timeout_seconds: int = 120,
        remove_orphans: bool = True,
    ) -> None:
        """使用固化 Compose 文件启动项目，并可等待 healthcheck 完成。"""
        _validate_project_name(project_name)
        if not files:
            raise ValueError("at least one Compose file is required")
        argv = self._compose_prefix(project_name, project_directory, files, env_file)
        argv.extend(("up", "--detach"))
        if remove_orphans:
            argv.append("--remove-orphans")
        if wait:
            if wait_timeout_seconds <= 0:
                raise ValueError("wait_timeout_seconds must be positive")
            argv.extend(("--wait", "--wait-timeout", str(wait_timeout_seconds)))
        await self._session.run(
            argv, timeout_seconds=wait_timeout_seconds + 30, check=True
        )

    async def compose_ps(
        self,
        *,
        project_name: str,
        project_directory: str,
        files: Sequence[str],
        env_file: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """查询 Compose 项目容器状态，供部署对账和健康检查使用。"""
        argv = self._compose_prefix(project_name, project_directory, files, env_file)
        argv.extend(("ps", "--all", "--format", "json"))
        result = await self._session.run(argv, check=True)
        return tuple(_load_json_collection(result.stdout))

    async def compose_down(
        self,
        *,
        project_name: str,
        project_directory: str,
        files: Sequence[str],
        env_file: str | None = None,
        remove_orphans: bool = True,
    ) -> None:
        """停止并清理 Compose 项目，可选移除孤儿容器。"""
        argv = self._compose_prefix(project_name, project_directory, files, env_file)
        argv.append("down")
        if remove_orphans:
            argv.append("--remove-orphans")
        await self._session.run(argv, check=True)

    async def compose_restart(
        self,
        *,
        project_name: str,
        project_directory: str,
        files: Sequence[str],
        env_file: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        """重启 Compose 项目并限制远端命令超时。"""
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("Compose restart timeout is out of range")
        argv = self._compose_prefix(project_name, project_directory, files, env_file)
        argv.extend(("restart", "--timeout", str(timeout_seconds)))
        await self._session.run(
            argv, timeout_seconds=timeout_seconds + 30, check=True
        )

    def _compose_prefix(
        self,
        project_name: str,
        project_directory: str,
        files: Sequence[str],
        env_file: str | None,
    ) -> list[str]:
        _validate_project_name(project_name)
        if not project_directory.startswith("/"):
            raise ValueError("Compose project directory must be absolute")
        argv = [
            *(
                ("env", f"DOCKER_CONFIG={self._docker_config}", "docker")
                if self._docker_config is not None
                else ("docker",)
            ),
            "compose",
            "--project-name",
            project_name,
            "--project-directory",
            project_directory,
        ]
        for path in files:
            if not path.startswith("/"):
                raise ValueError("Compose file paths must be absolute")
            argv.extend(("--file", path))
        if env_file is not None:
            if not env_file.startswith("/"):
                raise ValueError("Compose env file path must be absolute")
            argv.extend(("--env-file", env_file))
        return argv


def _container_summary(container: Any) -> ContainerSummary:
    attrs = container.attrs
    config = attrs.get("Config") or {}
    state = attrs.get("State") or {}
    mounts = tuple(
        MountSummary(
            type=str(mount.get("Type", "")),
            source=str(mount.get("Source", "")),
            destination=str(mount.get("Destination", "")),
            read_only=not bool(mount.get("RW", False)),
        )
        for mount in attrs.get("Mounts") or ()
    )
    return ContainerSummary(
        id=str(container.id),
        name=str(container.name),
        image=str(config.get("Image") or ""),
        status=str(container.status),
        state=str(state.get("Status") or container.status),
        created_at=attrs.get("Created"),
        mounts=mounts,
        labels=dict(config.get("Labels") or {}),
    )


def _image_summary(image: Any) -> ImageSummary:
    attrs = image.attrs
    return ImageSummary(
        id=str(image.id),
        tags=tuple(image.tags or ()),
        size=int(attrs.get("Size") or 0),
        created_at=attrs.get("Created"),
        labels=dict((attrs.get("Config") or {}).get("Labels") or {}),
    )


def _volume_summary(volume: Any) -> VolumeSummary:
    attrs = volume.attrs
    return VolumeSummary(
        name=str(attrs.get("Name") or volume.name),
        driver=str(attrs.get("Driver") or ""),
        mountpoint=str(attrs.get("Mountpoint") or ""),
        labels=dict(attrs.get("Labels") or {}),
    )


def _network_summary(network: Any) -> NetworkSummary:
    attrs = network.attrs
    return NetworkSummary(
        id=str(network.id),
        name=str(network.name),
        driver=str(attrs.get("Driver") or ""),
        scope=str(attrs.get("Scope") or ""),
        containers=len(attrs.get("Containers") or {}),
    )


def _validate_object_name(value: str, kind: str) -> None:
    if not _OBJECT_NAME.fullmatch(value):
        raise ValueError(f"invalid Docker {kind} name")


def _validate_project_name(value: str) -> None:
    if not _PROJECT_NAME.fullmatch(value):
        raise ValueError("invalid Compose project name")


def _load_json_object(value: bytes) -> Mapping[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def _load_json_lines(value: bytes) -> list[Mapping[str, Any]]:
    parsed: list[Mapping[str, Any]] = []
    for line in value.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("expected one JSON object per line")
        parsed.append(item)
    return parsed


def _load_json_collection(value: bytes) -> list[Mapping[str, Any]]:
    stripped = value.strip()
    if not stripped:
        return []
    parsed = json.loads(stripped)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        return parsed
    return _load_json_lines(value)
