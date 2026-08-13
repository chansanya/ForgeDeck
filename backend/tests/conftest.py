from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from devops.api.main import create_app
from devops.config import Settings

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"


@pytest_asyncio.fixture
async def app(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        secret_key_path=tmp_path / "master.key",
        template_dir=TEMPLATE_DIR,
        admin_username="admin",
        admin_initial_password="correct-horse-battery-staple",
        sse_poll_interval_seconds=0.01,
        mcp_allowed_hosts=["testserver", "testserver:*", "devops.test", "devops.test:*"],
    )
    application = create_app(settings)
    async with LifespanManager(application):
        yield application


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as test_client:
        yield test_client


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
