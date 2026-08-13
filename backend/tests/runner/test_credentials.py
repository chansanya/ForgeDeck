from __future__ import annotations

import base64
import json

import pytest

from devops.runner.credentials import (
    docker_config_bytes,
    parse_git_credentials,
    parse_registry_credentials,
    parse_ssh_credentials,
)


def test_parses_structured_ssh_private_key() -> None:
    credentials = parse_ssh_credentials(
        '{"private_key":"-----BEGIN PRIVATE KEY-----\\nkey", "passphrase":"secret"}'
    )
    assert credentials.private_key == "-----BEGIN PRIVATE KEY-----\nkey"
    assert credentials.passphrase == "secret"
    assert credentials.password is None


def test_plain_git_secret_is_treated_as_token() -> None:
    credentials = parse_git_credentials("token-value")
    assert credentials.username == "git"
    assert credentials.password == "token-value"


def test_invalid_structured_credential_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires password or private_key"):
        parse_ssh_credentials('{"username":"root"}')


def test_registry_credentials_support_plain_and_json_secrets() -> None:
    plain = parse_registry_credentials(
        "plain-password",
        {"username": "builder", "endpoint": "registry.example.test"},
    )
    structured = parse_registry_credentials(
        '{"username":"robot","password":"json-password","endpoint":"registry-2.example.test"}'
    )

    assert plain.username == "builder"
    assert structured.password == "json-password"
    config = json.loads(docker_config_bytes(plain, "registry.example.test/team/app:tag"))
    encoded = config["auths"]["registry.example.test"]["auth"]
    assert base64.b64decode(encoded).decode() == "builder:plain-password"
