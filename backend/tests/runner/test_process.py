from __future__ import annotations

import asyncio
import sys
from collections.abc import Hashable

from devops.runner.process import AsyncCommandRunner, CommandSpec


class RecordingSink:
    def __init__(self) -> None:
        self.streams: dict[str, set[Hashable]] = {"info": set(), "error": set()}
        self.flushed: set[Hashable] = set()

    async def write(
        self,
        data: bytes,
        *,
        level: str = "info",
        stage: str | None = None,
        stream_id: Hashable | None = None,
    ) -> None:
        assert data
        assert stage == "process-test"
        assert stream_id is not None
        self.streams[level].add(stream_id)

    async def flush(self, *, stream_id: Hashable) -> None:
        self.flushed.add(stream_id)


async def test_command_runner_passes_metacharacters_as_literal_arguments() -> None:
    marker = "literal;echo SHOULD_NOT_RUN"
    result = await AsyncCommandRunner().run(
        CommandSpec(
            argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", marker),
            timeout=10,
        )
    )

    assert result.ok
    assert result.stdout.decode().strip() == marker


async def test_command_runner_bounds_captured_output() -> None:
    result = await AsyncCommandRunner().run(
        CommandSpec(
            argv=(sys.executable, "-c", "print('x' * 100)"),
            timeout=10,
            max_capture_bytes=16,
        )
    )

    assert result.ok
    assert result.output_truncated
    assert len(result.stdout) == 16


async def test_command_runner_uses_independent_streams_and_flushes_them() -> None:
    sink = RecordingSink()
    result = await AsyncCommandRunner().run(
        CommandSpec(
            argv=(
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('out'); sys.stderr.write('err')",
            ),
            timeout=10,
            stage="process-test",
        ),
        sink=sink,
    )

    assert result.ok
    assert len(sink.streams["info"]) == 1
    assert len(sink.streams["error"]) == 1
    assert sink.streams["info"].isdisjoint(sink.streams["error"])
    assert sink.flushed == sink.streams["info"] | sink.streams["error"]


async def test_command_runner_cancels_process_group() -> None:
    cancel_event = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.05)
        cancel_event.set()

    cancel_task = asyncio.create_task(cancel_soon())
    result = await AsyncCommandRunner().run(
        CommandSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            timeout=10,
            terminate_grace_seconds=0.2,
        ),
        cancel_event=cancel_event,
    )
    await cancel_task

    assert result.cancelled
    assert not result.ok
    assert result.duration_seconds < 5
