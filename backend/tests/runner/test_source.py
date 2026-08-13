from __future__ import annotations

from pathlib import Path

import pytest

from devops.runner.process import CommandResult, CommandSpec
from devops.runner.source import GitSourceManager, canonical_snapshot, resolve_repository_path


class RecordingCommandRunner:
    def __init__(self, commit_sha: str) -> None:
        self.commit_sha = commit_sha
        self.specs: list[CommandSpec] = []

    async def run(self, spec: CommandSpec, **_: object) -> CommandResult:
        self.specs.append(spec)
        stdout = f"{self.commit_sha}\n".encode() if spec.argv[-2:] == ("rev-parse", "HEAD") else b""
        return CommandResult(
            argv=spec.argv,
            returncode=0,
            stdout=stdout,
            stderr=b"",
            duration_seconds=0.01,
        )


def test_snapshot_hash_is_canonical() -> None:
    first = canonical_snapshot({"b": 2, "a": {"z": 1}})
    second = canonical_snapshot({"a": {"z": 1}, "b": 2})
    assert first == second


def test_repository_path_rejects_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        resolve_repository_path(root, "../secret", must_exist=False)


def test_repository_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "source"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted on this platform")
    with pytest.raises(ValueError, match="escapes"):
        resolve_repository_path(root, "linked", must_exist=True)


async def test_checkout_rejects_non_http_repository_urls(tmp_path: Path) -> None:
    manager = GitSourceManager(RecordingCommandRunner("a" * 40))  # type: ignore[arg-type]
    for repo_url in (
        "ssh://git@example.test/repo.git",
        "git://example.test/repo.git",
        "git@example.test:team/repo.git",
    ):
        with pytest.raises(ValueError, match=r"HTTP\(S\)"):
            await manager.checkout(
                repo_url=repo_url,
                commit_sha="a" * 40,
                destination=tmp_path / repo_url.split(":", 1)[0],
            )


async def test_checkout_isolates_git_configuration_and_protocols(tmp_path: Path) -> None:
    commit_sha = "b" * 40
    commands = RecordingCommandRunner(commit_sha)
    manager = GitSourceManager(commands)  # type: ignore[arg-type]

    checkout = await manager.checkout(
        repo_url="https://git.example.test/team/repo.git",
        commit_sha=commit_sha,
        destination=tmp_path / "checkout",
        extra_env={"GIT_CONFIG_NOSYSTEM": "0", "GIT_PROTOCOL_FROM_USER": "1"},
    )

    assert checkout.commit_sha == commit_sha
    fetch = next(spec for spec in commands.specs if "fetch" in spec.argv)
    assert fetch.env is not None
    assert fetch.env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert fetch.env["GIT_PROTOCOL_FROM_USER"] == "0"
    assert "protocol.allow=never" in fetch.argv
    assert "protocol.http.allow=always" in fetch.argv
    assert "protocol.https.allow=always" in fetch.argv
