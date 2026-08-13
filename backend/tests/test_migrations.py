from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from devops.config import get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


def _alembic_config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DEVOPS_ENVIRONMENT", "development")
    monkeypatch.setenv(
        "DEVOPS_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    get_settings.cache_clear()
    return Config(str(ALEMBIC_INI))


def _assert_head_schema(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(projects)").fetchall()
        }
    assert revision == ("0005_environment_target",)
    assert "registry_credential_id" in columns
    with sqlite3.connect(database_path) as connection:
        deployment_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(deployments)").fetchall()
        }
        unique_index_columns = {
            tuple(
                column[2]
                for column in connection.execute(
                    f'PRAGMA index_info("{row[1]}")'
                ).fetchall()
            )
            for row in connection.execute(
                "PRAGMA index_list(deployment_environments)"
            ).fetchall()
            if row[2]
        }
    assert "previous_deployment_id" in deployment_columns
    assert ("server_id", "deploy_path") in unique_index_columns


def test_empty_database_upgrades_to_head_without_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "fresh.db"
    config = _alembic_config(database_path, monkeypatch)
    try:
        command.upgrade(config, "head")
        command.check(config)
        _assert_head_schema(database_path)
    finally:
        get_settings.cache_clear()


def test_upgrade_tolerates_tables_created_by_legacy_dynamic_initial_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "legacy.db"
    config = _alembic_config(database_path, monkeypatch)
    try:
        command.upgrade(config, "0002_notify_mcp_tokens")
        command.stamp(config, "0001_initial")
        command.upgrade(config, "head")
        command.check(config)
        _assert_head_schema(database_path)
    finally:
        get_settings.cache_clear()


def test_upgrade_rejects_duplicate_server_deploy_paths_with_clear_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "duplicate-targets.db"
    config = _alembic_config(database_path, monkeypatch)
    try:
        command.upgrade(config, "0004_deployment_previous")
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "INSERT INTO projects "
                "(id, name, repo_url, default_branch, enabled, dockerfile_source, "
                "dockerfile_path, build_context, build_args, pipeline_config, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    "project-1",
                    "project-1",
                    "https://example.test/one.git",
                    "main",
                    1,
                    "repository",
                    "Dockerfile",
                    ".",
                    "{}",
                    "{}",
                ),
            )
            connection.execute(
                "INSERT INTO projects "
                "(id, name, repo_url, default_branch, enabled, dockerfile_source, "
                "dockerfile_path, build_context, build_args, pipeline_config, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    "project-2",
                    "project-2",
                    "https://example.test/two.git",
                    "main",
                    1,
                    "repository",
                    "Dockerfile",
                    ".",
                    "{}",
                    "{}",
                ),
            )
            connection.execute(
                "INSERT INTO servers "
                "(id, name, host, port, username, labels, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("server-1", "server-1", "server-1", 22, "root", "{}", 1),
            )
            for index, project_id in enumerate(("project-1", "project-2"), start=1):
                connection.execute(
                    "INSERT INTO deployment_environments "
                    "(id, project_id, server_id, name, compose_source, compose_path, "
                    "deploy_path, env_config, healthcheck, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    (
                        f"environment-{index}",
                        project_id,
                        "server-1",
                        "production",
                        "repository",
                        "compose.yaml",
                        "/srv/shared",
                        "{}",
                        "{}",
                    ),
                )
            connection.commit()

        with pytest.raises(RuntimeError, match="resolve duplicate environments first"):
            command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()
