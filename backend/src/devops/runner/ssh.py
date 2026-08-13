"""实现强制主机指纹确认的 AsyncSSH 命令、SFTP 与 PTY 会话。"""

from __future__ import annotations

import asyncio
import contextlib
import posixpath
import shlex
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

try:
    import asyncssh
except ImportError:  # pragma: no cover - exercised by minimal API-only installations
    asyncssh = None  # type: ignore[assignment]


class SSHUnavailableError(RuntimeError):
    pass


class HostKeyVerificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SSHConnectionConfig:
    host: str
    username: str
    host_key: str
    port: int = 22
    connect_timeout: float = 15.0
    keepalive_interval: float = 15.0
    keepalive_count_max: int = 3

    def __post_init__(self) -> None:
        if not self.host.strip() or not self.username.strip():
            raise ValueError("SSH host and username are required")
        if not self.host_key.strip():
            raise ValueError("a pinned SSH host key or SHA256 fingerprint is required")
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH port is out of range")
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")


@dataclass(frozen=True, slots=True)
class SSHCredentials:
    password: str | None = None
    client_keys: tuple[Any, ...] = ()
    private_key: str | bytes | None = None
    passphrase: str | None = None


@dataclass(frozen=True, slots=True)
class SSHHostKeyScan:
    algorithm: str
    fingerprint: str
    public_key: str


@dataclass(frozen=True, slots=True)
class SSHCommandResult:
    argv: tuple[str, ...]
    exit_status: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    def check_returncode(self) -> SSHCommandResult:
        if not self.ok:
            raise SSHCommandError(self)
        return self


class SSHCommandError(RuntimeError):
    def __init__(self, result: SSHCommandResult) -> None:
        self.result = result
        super().__init__(
            f"remote command failed with exit status {result.exit_status}: {result.argv[0]}"
        )


class SSHTerminal(Protocol):
    async def read(self, size: int = 65536) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    async def resize(self, columns: int, rows: int) -> None: ...

    async def wait_closed(self) -> int: ...

    async def close(self) -> None: ...


class SSHSession(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: float | None = None,
        check: bool = False,
    ) -> SSHCommandResult: ...

    async def write_file_atomic(
        self, remote_path: str, data: bytes, *, mode: int = 0o600
    ) -> None: ...

    async def read_file(self, remote_path: str, *, max_bytes: int = 4 * 1024 * 1024) -> bytes: ...

    async def exists(self, remote_path: str) -> bool: ...

    async def remove_file(self, remote_path: str) -> None: ...

    async def open_terminal(
        self,
        *,
        term_type: str = "xterm-256color",
        columns: int = 120,
        rows: int = 32,
    ) -> SSHTerminal: ...


class SSHConnector(Protocol):
    def connect(
        self, config: SSHConnectionConfig, credentials: SSHCredentials
    ) -> AsyncIterator[SSHSession]: ...


def _normalise_public_key(value: str) -> str:
    fields = value.strip().split()
    if len(fields) >= 2:
        for index, field in enumerate(fields[:-1]):
            if field.startswith(("ssh-", "ecdsa-", "sk-")):
                return f"{field} {fields[index + 1]}"
    return " ".join(fields)


def _presented_key_values(key: Any) -> frozenset[str]:
    values: set[str] = set()
    for algorithm in ("sha256", "md5"):
        with contextlib.suppress(Exception):
            values.add(str(key.get_fingerprint(algorithm)).strip())
    with contextlib.suppress(Exception):
        exported = key.export_public_key("openssh")
        if isinstance(exported, bytes):
            exported = exported.decode()
        values.add(_normalise_public_key(str(exported)))
    return frozenset(values)


if asyncssh is not None:

    class _PinnedSSHClient(asyncssh.SSHClient):  # type: ignore[misc]
        def __init__(self, pinned_host_key: str) -> None:
            self._pinned_host_key = pinned_host_key.strip()

        def validate_host_public_key(
            self, host: str, addr: str, port: int, key: Any
        ) -> bool:
            expected = self._pinned_host_key
            if not expected.startswith(("SHA256:", "MD5:")):
                expected = _normalise_public_key(expected)
            valid = expected in _presented_key_values(key)
            if not valid:
                raise HostKeyVerificationError(
                    f"SSH host key mismatch for {host}:{port}; refusing authentication"
                )
            return True


class AsyncSSHTerminal:
    def __init__(self, process: Any) -> None:
        self._process = process

    async def read(self, size: int = 65536) -> bytes:
        value = await self._process.stdout.read(size)
        return value.encode() if isinstance(value, str) else value

    async def write(self, data: bytes) -> None:
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def resize(self, columns: int, rows: int) -> None:
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal size must be positive")
        self._process.change_terminal_size(columns, rows)

    async def wait_closed(self) -> int:
        await self._process.wait_closed()
        return int(self._process.exit_status or 0)

    async def close(self) -> None:
        self._process.terminate()
        await self._process.wait_closed()


class AsyncSSHSession:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: float | None = None,
        check: bool = False,
    ) -> SSHCommandResult:
        """将远端参数数组安全引用后执行，避免 shell 注入并可选检查退出码。"""
        safe_argv = _validated_argv(argv)
        result = await self._connection.run(
            shlex.join(safe_argv),
            input=stdin,
            timeout=timeout_seconds,
            check=False,
            encoding=None,
        )
        command_result = SSHCommandResult(
            argv=safe_argv,
            exit_status=int(result.exit_status),
            stdout=_as_bytes(result.stdout),
            stderr=_as_bytes(result.stderr),
        )
        if check:
            command_result.check_returncode()
        return command_result

    async def write_file_atomic(
        self, remote_path: str, data: bytes, *, mode: int = 0o600
    ) -> None:
        """通过临时文件和原子移动上传配置，避免远端读取到半写内容。"""
        path = _validated_remote_path(remote_path)
        parent = posixpath.dirname(path)
        await self.run(("mkdir", "-p", "--", parent), check=True)
        temporary = f"{path}.tmp-{uuid.uuid4().hex}"
        sftp = await self._connection.start_sftp_client()
        try:
            async with sftp.open(temporary, "wb") as handle:
                await handle.write(data)
            await sftp.chmod(temporary, mode)
            await self.run(("mv", "-f", "--", temporary, path), check=True)
        except Exception:
            with contextlib.suppress(Exception):
                await sftp.remove(temporary)
            raise
        finally:
            sftp.exit()
            await sftp.wait_closed()

    async def read_file(self, remote_path: str, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        """读取受大小上限保护的远端文件，防止意外消耗 Runner 内存。"""
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        path = _validated_remote_path(remote_path)
        sftp = await self._connection.start_sftp_client()
        try:
            attrs = await sftp.stat(path)
            if attrs.size is not None and attrs.size > max_bytes:
                raise ValueError(f"remote file exceeds {max_bytes} bytes: {path}")
            async with sftp.open(path, "rb") as handle:
                value = await handle.read(max_bytes + 1)
            value = _as_bytes(value)
            if len(value) > max_bytes:
                raise ValueError(f"remote file exceeds {max_bytes} bytes: {path}")
            return value
        finally:
            sftp.exit()
            await sftp.wait_closed()

    async def exists(self, remote_path: str) -> bool:
        """检查远端路径是否存在，并将文件不存在转换为 False。"""
        path = _validated_remote_path(remote_path)
        sftp = await self._connection.start_sftp_client()
        try:
            try:
                await sftp.stat(path)
            except (FileNotFoundError, OSError):
                return False
            return True
        finally:
            sftp.exit()
            await sftp.wait_closed()

    async def remove_file(self, remote_path: str) -> None:
        """删除经绝对路径校验的远端控制文件。"""
        path = _validated_remote_path(remote_path)
        await self.run(("rm", "-f", "--", path), check=True)

    async def open_terminal(
        self,
        *,
        term_type: str = "xterm-256color",
        columns: int = 120,
        rows: int = 32,
    ) -> AsyncSSHTerminal:
        """打开带 PTY 的交互终端，并校验初始尺寸为正数。"""
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal size must be positive")
        process = await self._connection.create_process(
            term_type=term_type,
            term_size=(columns, rows),
            encoding=None,
        )
        return AsyncSSHTerminal(process)


class AsyncSSHConnector:
    async def scan_host_key(
        self,
        host: str,
        port: int = 22,
        *,
        timeout_seconds: float = 10,
    ) -> SSHHostKeyScan:
        """获取主机公钥和指纹；登记前由管理员确认结果。"""
        if asyncssh is None:
            raise SSHUnavailableError(
                "AsyncSSH is not installed; install the project with the 'runner' extra"
            )
        if (
            not host
            or host != host.strip()
            or any(character.isspace() or ord(character) < 32 for character in host)
            or any(character in host for character in "/\\@?#")
        ):
            raise ValueError("host is invalid")
        if not 1 <= port <= 65535:
            raise ValueError("port is out of range")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(host, port=port),
            timeout=timeout_seconds,
        )
        if key is None:
            raise RuntimeError("SSH server did not present a host key")
        return SSHHostKeyScan(
            algorithm=key.get_algorithm(),
            fingerprint=key.get_fingerprint("sha256"),
            public_key=key.export_public_key("openssh").decode("ascii").strip(),
        )

    @asynccontextmanager
    async def connect(
        self, config: SSHConnectionConfig, credentials: SSHCredentials
    ) -> AsyncIterator[AsyncSSHSession]:
        """建立强制匹配已登记指纹的 SSH 连接，断开时释放 AsyncSSH 资源。"""
        if asyncssh is None:
            raise SSHUnavailableError(
                "AsyncSSH is not installed; install the project with the 'runner' extra"
            )
        client_keys = list(credentials.client_keys)
        if credentials.private_key is not None:
            client_keys.append(
                asyncssh.import_private_key(
                    credentials.private_key,
                    passphrase=credentials.passphrase,
                )
            )
        connection = await asyncssh.connect(
            config.host,
            port=config.port,
            username=config.username,
            password=credentials.password,
            client_keys=client_keys or None,
            passphrase=credentials.passphrase,
            # A non-empty, seven-list known_hosts result keeps AsyncSSH's host-key
            # validation path enabled while delegating the actual pin match to the
            # client callback. Passing None would silently disable that callback.
            known_hosts=((), (), (), (), (), (), ()),
            client_factory=lambda: _PinnedSSHClient(config.host_key),
            connect_timeout=config.connect_timeout,
            keepalive_interval=config.keepalive_interval,
            keepalive_count_max=config.keepalive_count_max,
            encoding=None,
        )
        try:
            yield AsyncSSHSession(connection)
        finally:
            connection.close()
            await connection.wait_closed()


def _validated_argv(argv: Sequence[str]) -> tuple[str, ...]:
    values = tuple(argv)
    if not values or not values[0]:
        raise ValueError("remote argv must contain an executable")
    if any("\x00" in value for value in values):
        raise ValueError("remote argv cannot contain NUL bytes")
    return values


def _validated_remote_path(path: str) -> str:
    if not path.startswith("/") or "\x00" in path:
        raise ValueError("remote paths must be absolute and cannot contain NUL bytes")
    normalised = posixpath.normpath(path)
    if normalised == "/":
        raise ValueError("the remote root directory cannot be used as a file path")
    return normalised


def _as_bytes(value: str | bytes | None) -> bytes:
    if value is None:
        return b""
    return value.encode() if isinstance(value, str) else bytes(value)
