"""以参数数组安全执行子进程，并处理超时、取消、限流和输出脱敏。

本模块禁止 shell 字符串执行，避免仓库配置进入命令解释器造成注入。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProcessOutputSink(Protocol):
    async def write(
        self,
        data: bytes,
        *,
        level: str = "info",
        stage: str | None = None,
        stream_id: Hashable | None = None,
    ) -> None:
        ...

    async def flush(self, *, stream_id: Hashable) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    stdin: bytes | None = None
    timeout: float | None = None
    terminate_grace_seconds: float = 5.0
    max_capture_bytes: int = 1024 * 1024
    stage: str | None = None

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0]:
            raise ValueError("argv must contain an executable")
        if any("\x00" in value for value in self.argv):
            raise ValueError("argv cannot contain NUL bytes")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.terminate_grace_seconds < 0:
            raise ValueError("terminate_grace_seconds cannot be negative")
        if self.max_capture_bytes <= 0:
            raise ValueError("max_capture_bytes must be positive")

    @classmethod
    def from_argv(cls, argv: Sequence[str], **kwargs: object) -> CommandSpec:
        """将任意序列固化为不可变参数元组，统一进入安全命令执行器。"""
        return cls(tuple(argv), **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled

    def check_returncode(self) -> CommandResult:
        """失败、超时或取消时抛出携带原始结果的统一异常。"""
        if not self.ok:
            raise CommandExecutionError(self)
        return self


class CommandExecutionError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        reason = "timed out" if result.timed_out else "cancelled" if result.cancelled else "failed"
        super().__init__(f"command {reason} with exit code {result.returncode}: {result.argv[0]}")


class _TailBuffer:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._value = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        if len(chunk) >= self._limit:
            self._value[:] = chunk[-self._limit :]
            self.truncated = True
            return
        overflow = len(self._value) + len(chunk) - self._limit
        if overflow > 0:
            del self._value[:overflow]
            self.truncated = True
        self._value.extend(chunk)

    def bytes(self) -> bytes:
        return bytes(self._value)


class AsyncCommandRunner:
    """Executes argument arrays only; a shell is never involved."""

    async def run(
        self,
        spec: CommandSpec,
        *,
        sink: ProcessOutputSink | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> CommandResult:
        """执行无 shell 子进程，持续排空输出并处理超时、取消和进程组终止。"""
        started = time.monotonic()
        environment = None
        if spec.env is not None:
            environment = os.environ.copy()
            environment.update(spec.env)

        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=str(spec.cwd) if spec.cwd is not None else None,
            env=environment,
            stdin=asyncio.subprocess.PIPE if spec.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        if spec.stdin is not None and process.stdin is not None:
            process.stdin.write(spec.stdin)
            await process.stdin.drain()
            process.stdin.close()

        stdout = _TailBuffer(spec.max_capture_bytes)
        stderr = _TailBuffer(spec.max_capture_bytes)
        readers = (
            asyncio.create_task(
                self._drain(process.stdout, stdout, sink, level="info", stage=spec.stage)
            ),
            asyncio.create_task(
                self._drain(process.stderr, stderr, sink, level="error", stage=spec.stage)
            ),
        )
        wait_task = asyncio.create_task(process.wait())
        cancel_task = (
            asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        )
        timed_out = False
        cancelled = False

        try:
            wait_set = {wait_task}
            if cancel_task is not None:
                wait_set.add(cancel_task)
            done, _ = await asyncio.wait(
                wait_set,
                timeout=spec.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                timed_out = True
                await self._terminate_process_group(process, spec.terminate_grace_seconds)
            elif cancel_task is not None and cancel_task in done and not wait_task.done():
                cancelled = True
                await self._terminate_process_group(process, spec.terminate_grace_seconds)
            await wait_task
        except asyncio.CancelledError:
            await self._terminate_process_group(process, spec.terminate_grace_seconds)
            raise
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
            await asyncio.gather(*readers)

        return CommandResult(
            argv=spec.argv,
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout.bytes(),
            stderr=stderr.bytes(),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=stdout.truncated or stderr.truncated,
        )

    async def _drain(
        self,
        stream: asyncio.StreamReader | None,
        capture: _TailBuffer,
        sink: ProcessOutputSink | None,
        *,
        level: str,
        stage: str | None,
    ) -> None:
        """独立读取一个输出流，日志失败时仍继续排空管道避免死锁。"""
        if stream is None:
            return
        # stdout and stderr are independent byte streams with no reliable total
        # ordering, so each pipe must own an independent redaction state.
        stream_id = object()
        active_sink = sink
        logging_failed = False
        try:
            while chunk := await stream.read(64 * 1024):
                capture.append(chunk)
                if active_sink is None or logging_failed:
                    continue
                try:
                    await active_sink.write(
                        chunk,
                        level=level,
                        stage=stage,
                        stream_id=stream_id,
                    )
                except Exception:
                    # Logging must never stop draining a subprocess pipe, otherwise
                    # a full pipe can deadlock the build/deployment command.
                    logging_failed = True
        finally:
            if active_sink is not None:
                with contextlib.suppress(Exception):
                    await active_sink.flush(stream_id=stream_id)

    async def _terminate_process_group(
        self, process: asyncio.subprocess.Process, grace_seconds: float
    ) -> None:
        """先优雅终止整个进程组，超时后再强制杀死，避免遗留构建子进程。"""
        if process.returncode is not None:
            return
        if os.name == "nt":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)

        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            return
        except TimeoutError:
            pass

        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
