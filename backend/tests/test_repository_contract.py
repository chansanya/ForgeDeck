from __future__ import annotations

import os
from datetime import timedelta

from devops.db.engine import Database
from devops.db.repositories import RunnerTaskRepository
from devops.domain.models import RunnerTask, TaskKind, TaskState, utcnow


async def _exercise_runner_task_contract(database_url: str) -> None:
    database = Database(database_url)
    try:
        await database.drop_schema()
        await database.create_schema()
        async with database.session_factory() as session:
            first = RunnerTask(
                kind=TaskKind.SCRIPT,
                resource_key="server:contract:script",
                payload={"sequence": 1},
            )
            second = RunnerTask(
                kind=TaskKind.SCRIPT,
                resource_key="server:contract:script",
                payload={"sequence": 2},
            )
            session.add_all((first, second))
            await session.commit()

        async with database.session_factory() as session:
            repository = RunnerTaskRepository(session)
            lease = await repository.lease_next("runner-a", lease_seconds=30)
            assert lease is not None
            assert lease.state == TaskState.LEASED
            await session.commit()

        async with database.session_factory() as session:
            repository = RunnerTaskRepository(session)
            blocked = await repository.lease_next("runner-b", lease_seconds=30)
            assert blocked is None
            await session.rollback()

        async with database.session_factory() as session:
            leased = await session.get(RunnerTask, lease.id)
            assert leased is not None
            leased.lease_expires_at = utcnow() - timedelta(seconds=1)
            await session.commit()

        async with database.session_factory() as session:
            repository = RunnerTaskRepository(session)
            recovered = await repository.recover_expired()
            assert recovered == 1
            await session.commit()
            next_lease = await repository.lease_next("runner-b", lease_seconds=30)
            assert next_lease is not None
            assert next_lease.resource_key == "server:contract:script"
            await session.commit()
    finally:
        await database.drop_schema()
        await database.dispose()


async def test_sqlite_and_optional_postgres_repository_contract(tmp_path) -> None:
    urls = [f"sqlite+aiosqlite:///{(tmp_path / 'contract.db').as_posix()}"]
    postgres_url = os.getenv("DEVOPS_POSTGRES_TEST_URL")
    if postgres_url:
        urls.append(postgres_url)
    for url in urls:
        await _exercise_runner_task_contract(url)
