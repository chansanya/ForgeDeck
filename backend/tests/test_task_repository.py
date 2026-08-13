from __future__ import annotations

from datetime import timedelta

from devops.db import Database, RunnerTaskRepository
from devops.domain.models import RunnerTask, TaskKind, TaskState, utcnow


async def test_task_lease_retry_and_expired_recovery(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'queue.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            session.add(
                RunnerTask(
                    kind=TaskKind.PIPELINE,
                    resource_key="project:1",
                    payload={"run_id": "r1"},
                )
            )
            await session.commit()

        async with database.session_factory() as session:
            repository = RunnerTaskRepository(session)
            leased = await repository.lease_next("runner-1", lease_seconds=30)
            assert leased is not None
            assert leased.state == TaskState.LEASED
            assert await repository.mark_running(leased.id, "runner-1")
            assert await repository.retry(
                leased.id,
                "runner-1",
                available_at=utcnow() - timedelta(seconds=1),
                error_message="temporary",
            )
            await session.commit()

        async with database.session_factory() as session:
            repository = RunnerTaskRepository(session)
            leased = await repository.lease_next("runner-2", lease_seconds=30)
            assert leased is not None
            leased.lease_expires_at = utcnow() - timedelta(seconds=1)
            await session.commit()

        async with database.session_factory() as session:
            repository = RunnerTaskRepository(session)
            assert await repository.recover_expired() == 1
            await session.commit()
            recovered = await session.get(RunnerTask, leased.id)
            assert recovered is not None
            assert recovered.state == TaskState.PENDING
            assert recovered.leased_by is None
    finally:
        await database.dispose()
