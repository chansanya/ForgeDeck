"""对 Runner 输出执行跨分块脱敏、容量限制并生成结构化日志事件。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from devops.runner.contracts import LogEvent, LogEventStore

_TRUNCATED_MESSAGE = "[runner log limit reached; further output omitted]"
_REDACTION = b"***"


class SecretRedactor:
    def __init__(self, secrets: Iterable[str | bytes] = ()) -> None:
        values: list[bytes] = []
        for secret in secrets:
            encoded = secret.encode() if isinstance(secret, str) else bytes(secret)
            if encoded:
                values.append(encoded)
        self._secrets = tuple(sorted(set(values), key=len, reverse=True))

    def add(self, secrets: Iterable[str | bytes]) -> None:
        """加入待脱敏密钥，并忽略空值以免污染匹配器。"""
        values = list(self._secrets)
        for secret in secrets:
            encoded = secret.encode() if isinstance(secret, str) else bytes(secret)
            if encoded:
                values.append(encoded)
        self._secrets = tuple(sorted(set(values), key=len, reverse=True))

    def redact(self, value: bytes) -> bytes:
        """对完整日志块执行密钥替换，返回不含明文凭据的字节串。"""
        redacted, _ = self._redact_prefix(value, len(value))
        return redacted

    def stream(self) -> StreamingSecretRedactor:
        """创建独立流式脱敏器，避免不同 stdout/stderr 流互相串状态。"""
        return StreamingSecretRedactor(self)

    @property
    def max_secret_length(self) -> int:
        """返回当前最长密钥长度，供流式边界保留策略使用。"""
        return len(self._secrets[0]) if self._secrets else 0

    def _redact_prefix(self, value: bytes, safe_start_limit: int) -> tuple[bytes, int]:
        """Redact matches whose starting position cannot depend on future input."""

        output = bytearray()
        position = 0
        while position < safe_start_limit:
            match = next(
                (secret for secret in self._secrets if value.startswith(secret, position)),
                None,
            )
            if match is None:
                output.append(value[position])
                position += 1
                continue
            output.extend(_REDACTION)
            position += len(match)
        return bytes(output), position


class StreamingSecretRedactor:
    """Redacts a stream while retaining at most ``max_secret_length - 1`` bytes."""

    def __init__(self, redactor: SecretRedactor) -> None:
        self._redactor = redactor
        self._pending = bytearray()
        self._closed = False

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    def feed(self, value: bytes) -> bytes:
        if self._closed:
            raise RuntimeError("redaction stream is closed")
        self._pending.extend(value)
        # 保留最长密钥减一字节，避免密钥恰好跨 stdout/stderr 分块时漏出前后半段。
        retained_bytes = max(0, self._redactor.max_secret_length - 1)
        safe_start_limit = max(0, len(self._pending) - retained_bytes)
        redacted, consumed = self._redactor._redact_prefix(
            bytes(self._pending), safe_start_limit
        )
        del self._pending[:consumed]
        return redacted

    def flush(self) -> bytes:
        if self._closed:
            return b""
        self._closed = True
        redacted = self._redactor.redact(bytes(self._pending))
        self._pending.clear()
        return redacted


@dataclass(slots=True)
class _LogStream:
    redactor: StreamingSecretRedactor
    level: str
    stage: str | None


class BoundedLogWriter:
    """Writes bounded, redacted task logs without retaining an unbounded buffer."""

    def __init__(
        self,
        store: LogEventStore,
        *,
        task_id: str,
        run_id: str | None,
        max_total_bytes: int = 16 * 1024 * 1024,
        max_event_bytes: int = 64 * 1024,
        secrets: Iterable[str | bytes] = (),
    ) -> None:
        if max_total_bytes <= 0 or max_event_bytes <= 0:
            raise ValueError("log limits must be positive")
        self._store = store
        self._task_id = task_id
        self._run_id = run_id
        self._max_total_bytes = max_total_bytes
        self._max_event_bytes = min(max_event_bytes, max_total_bytes)
        self._redactor = SecretRedactor(secrets)
        self._streams: dict[Hashable, _LogStream] = {}
        self._written_bytes = 0
        self._limit_reported = False
        self._lock = asyncio.Lock()

    @property
    def written_bytes(self) -> int:
        return self._written_bytes

    def add_secrets(self, secrets: Iterable[str | bytes]) -> None:
        """更新写入器使用的密钥集合，并同步刷新流式脱敏器。"""
        self._redactor.add(secrets)

    async def write(
        self,
        data: str | bytes,
        *,
        level: str = "info",
        stage: str | None = None,
        stream_id: Hashable | None = None,
    ) -> None:
        """脱敏并写入有界日志；超过上限时只保留截断标记。"""
        raw = data.encode(errors="replace") if isinstance(data, str) else bytes(data)
        async with self._lock:
            if stream_id is None:
                redacted = self._redactor.redact(raw)
            else:
                stream = self._streams.get(stream_id)
                if stream is None:
                    stream = _LogStream(self._redactor.stream(), level, stage)
                    self._streams[stream_id] = stream
                elif stream.level != level or stream.stage != stage:
                    raise ValueError("a log stream cannot change level or stage")
                redacted = stream.redactor.feed(raw)
            await self._write_redacted(redacted, level=level, stage=stage)

    async def flush(self, *, stream_id: Hashable) -> None:
        async with self._lock:
            stream = self._streams.pop(stream_id, None)
            if stream is None:
                return
            await self._write_redacted(
                stream.redactor.flush(), level=stream.level, stage=stream.stage
            )

    async def _write_redacted(
        self, raw: bytes, *, level: str, stage: str | None
    ) -> None:
        offset = 0
        while offset < len(raw):
            remaining = self._max_total_bytes - self._written_bytes
            if remaining <= 0:
                await self._report_limit_once(stage)
                return
            accepted = raw[offset : offset + min(remaining, self._max_event_bytes)]
            offset += len(accepted)
            self._written_bytes += len(accepted)
            await self._store.append(
                LogEvent(
                    run_id=self._run_id,
                    task_id=self._task_id,
                    level=level,
                    stage=stage,
                    message=accepted.decode(errors="replace"),
                    created_at=datetime.now(UTC),
                )
            )

    async def _report_limit_once(self, stage: str | None) -> None:
        if self._limit_reported:
            return
        self._limit_reported = True
        await self._store.append(
            LogEvent(
                run_id=self._run_id,
                task_id=self._task_id,
                level="warning",
                stage=stage,
                message=_TRUNCATED_MESSAGE,
                created_at=datetime.now(UTC),
            )
        )


class BoundedEventBuffer:
    """Small in-memory tail suitable for fan-out to SSE subscribers."""

    def __init__(self, max_events: int = 512) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self._events: deque[LogEvent] = deque(maxlen=max_events)
        self._condition = asyncio.Condition()
        self._sequence = 0

    async def append(self, event: LogEvent) -> None:
        """追加事件并唤醒等待者，缓冲超限时丢弃最旧事件而非无限增长。"""
        async with self._condition:
            self._events.append(event)
            self._sequence += 1
            self._condition.notify_all()

    async def snapshot(self) -> tuple[int, tuple[LogEvent, ...]]:
        """返回当前事件序号和快照，供 SSE 重放使用。"""
        async with self._condition:
            return self._sequence, tuple(self._events)

    async def wait_for_change(
        self, sequence: int, *, wait_timeout: float | None = None
    ) -> tuple[int, tuple[LogEvent, ...]]:
        """等待序号变化或超时，避免 SSE 客户端永久占用协程。"""
        async with self._condition:
            if self._sequence == sequence:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._sequence != sequence),
                    timeout=wait_timeout,
                )
            return self._sequence, tuple(self._events)
