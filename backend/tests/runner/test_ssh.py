from __future__ import annotations

from typing import Any

import pytest

from devops.runner import ssh
from devops.runner.ssh import AsyncSSHConnector, SSHConnectionConfig, SSHCredentials


class FakeConnection:
    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class FakeHostKey:
    def get_algorithm(self) -> str:
        return "ssh-ed25519"

    def get_fingerprint(self, hash_name: str = "sha256") -> str:
        assert hash_name == "sha256"
        return "SHA256:candidate"

    def export_public_key(self, format_name: str = "openssh") -> bytes:
        assert format_name == "openssh"
        return b"ssh-ed25519 AAAAC3NzaCandidate\n"


async def test_connector_keeps_asyncssh_host_key_validation_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_connect(host: str, **kwargs: Any) -> FakeConnection:
        captured.update(kwargs)
        return FakeConnection()

    assert ssh.asyncssh is not None
    monkeypatch.setattr(ssh.asyncssh, "connect", fake_connect)
    connector = AsyncSSHConnector()
    config = SSHConnectionConfig(
        host="server.example.test",
        username="root",
        host_key="SHA256:expected",
    )
    async with connector.connect(config, SSHCredentials()):
        pass

    assert captured["known_hosts"] == ((), (), (), (), (), (), ())
    client = captured["client_factory"]()
    assert client is not None


def test_ssh_config_refuses_unpinned_host() -> None:
    with pytest.raises(ValueError, match="pinned SSH host key"):
        SSHConnectionConfig(host="server", username="root", host_key="")


async def test_host_key_scan_returns_fingerprint_and_public_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_scan(host: str, port: int = 22) -> FakeHostKey:
        assert (host, port) == ("server.example.test", 2222)
        return FakeHostKey()

    assert ssh.asyncssh is not None
    monkeypatch.setattr(ssh.asyncssh, "get_server_host_key", fake_scan)
    result = await AsyncSSHConnector().scan_host_key("server.example.test", 2222)

    assert result.algorithm == "ssh-ed25519"
    assert result.fingerprint == "SHA256:candidate"
    assert result.public_key == "ssh-ed25519 AAAAC3NzaCandidate"
