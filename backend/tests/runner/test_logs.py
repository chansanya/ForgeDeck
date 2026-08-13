from __future__ import annotations

from devops.runner.contracts import LogEvent
from devops.runner.logs import BoundedLogWriter, SecretRedactor


class MemoryLogStore:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    async def append(self, event: LogEvent) -> None:
        self.events.append(event)


async def test_log_writer_redacts_and_caps_output() -> None:
    store = MemoryLogStore()
    writer = BoundedLogWriter(
        store,
        task_id="task-1",
        run_id="run-1",
        max_total_bytes=12,
        max_event_bytes=12,
        secrets=("token-123",),
    )

    await writer.write("token-123:abcdefghijkl")
    await writer.write("ignored")

    assert store.events[0].message == "***:abcdefgh"
    assert store.events[1].level == "warning"
    assert "limit reached" in store.events[1].message
    assert len(store.events) == 2
    assert writer.written_bytes == 12


def test_streaming_redactor_handles_boundaries_multiple_secrets_and_flush() -> None:
    redactor = SecretRedactor(("long-token-123", "short-secret"))
    stream = redactor.stream()

    output = b"".join(
        (
            stream.feed(b"before long-to"),
            stream.feed(b"ken-123 middle short-"),
            stream.feed(b"secret trailing"),
            stream.flush(),
        )
    )

    assert output == b"before *** middle *** trailing"
    assert stream.pending_bytes == 0
    assert stream.flush() == b""


def test_streaming_redactor_flushes_incomplete_secret_prefix_without_loss() -> None:
    stream = SecretRedactor(("token-123",)).stream()

    output = stream.feed(b"plain token-") + stream.flush()

    assert output == b"plain token-"


def test_streaming_redactor_matches_batch_redaction_for_every_chunk_boundary() -> None:
    value = b"prefix token-123 middle api-key suffix"
    redactor = SecretRedactor(("token-123", "api-key"))
    expected = redactor.redact(value)

    for first_boundary in range(len(value) + 1):
        for second_boundary in range(first_boundary, len(value) + 1):
            stream = redactor.stream()
            output = b"".join(
                (
                    stream.feed(value[:first_boundary]),
                    stream.feed(value[first_boundary:second_boundary]),
                    stream.feed(value[second_boundary:]),
                    stream.flush(),
                )
            )
            assert output == expected


async def test_log_writer_keeps_stdout_and_stderr_redaction_state_separate() -> None:
    store = MemoryLogStore()
    writer = BoundedLogWriter(
        store,
        task_id="task-1",
        run_id="run-1",
        secrets=("token-123",),
    )
    stdout_stream = object()
    stderr_stream = object()

    await writer.write(b"stdout token-", stream_id=stdout_stream)
    await writer.write(b"stderr token-", level="error", stream_id=stderr_stream)
    await writer.write(b"123 done", stream_id=stdout_stream)
    await writer.write(b"123 failed", level="error", stream_id=stderr_stream)
    await writer.flush(stream_id=stdout_stream)
    await writer.flush(stream_id=stderr_stream)

    stdout = "".join(event.message for event in store.events if event.level == "info")
    stderr = "".join(event.message for event in store.events if event.level == "error")
    assert stdout == "stdout *** done"
    assert stderr == "stderr *** failed"
