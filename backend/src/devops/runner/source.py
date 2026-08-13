"""检出固化的 Git commit，并限制仓库协议与仓库内路径边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from devops.runner.process import AsyncCommandRunner, CommandSpec, ProcessOutputSink

_COMMIT_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


@dataclass(frozen=True, slots=True)
class SourceCheckout:
    directory: Path
    commit_sha: str


class GitSourceManager:
    def __init__(self, command_runner: AsyncCommandRunner) -> None:
        """注入受控命令执行器，供检出流程复用统一超时和日志策略。"""
        self._commands = command_runner

    async def checkout(
        self,
        *,
        repo_url: str,
        commit_sha: str,
        destination: Path,
        sink: ProcessOutputSink | None = None,
        cancel_event: Any = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> SourceCheckout:
        """以固定 commit 检出仓库，禁用交互和危险 Git 协议并复核最终 HEAD。"""
        repo_url = _validated_repo_url(repo_url)
        if not _COMMIT_SHA.fullmatch(commit_sha):
            raise ValueError("commit_sha must be a hexadecimal Git object ID")
        await asyncio.to_thread(_prepare_destination, destination)
        environment = {
            **(extra_env or {}),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_PROTOCOL_FROM_USER": "0",
        }
        # 固定 commit 并显式关闭额外 Git 协议，避免分支漂移、交互阻塞和
        # 仓库配置通过 file/ext 等协议读取控制机本地资源。
        commands = (
            ("git", "init", "--quiet", str(destination)),
            ("git", "-C", str(destination), "remote", "add", "origin", repo_url),
            (
                "git",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.http.allow=always",
                "-c",
                "protocol.https.allow=always",
                "-C",
                str(destination),
                "fetch",
                "--quiet",
                "--no-tags",
                "--depth",
                "1",
                "origin",
                commit_sha,
            ),
            (
                "git",
                "-C",
                str(destination),
                "checkout",
                "--quiet",
                "--detach",
                "FETCH_HEAD",
            ),
        )
        for argv in commands:
            result = await self._commands.run(
                CommandSpec(argv=argv, env=environment, timeout=300, stage="checkout"),
                sink=sink,
                cancel_event=cancel_event,
            )
            result.check_returncode()
        head = await self._commands.run(
            CommandSpec(
                argv=("git", "-C", str(destination), "rev-parse", "HEAD"),
                env=environment,
                timeout=30,
                stage="checkout",
            ),
            sink=sink,
            cancel_event=cancel_event,
        )
        head.check_returncode()
        actual_sha = head.stdout.decode().strip().lower()
        if actual_sha != commit_sha.lower():
            raise RuntimeError(
                f"Git checkout mismatch: requested {commit_sha.lower()}, got {actual_sha}"
            )
        return SourceCheckout(directory=destination, commit_sha=actual_sha)


def resolve_repository_path(root: Path, relative_path: str, *, must_exist: bool = True) -> Path:
    """解析仓库内相对路径，并在跟随符号链接后阻止目录边界逃逸。"""
    if not relative_path or "\x00" in relative_path:
        raise ValueError("repository path is invalid")
    candidate_path = Path(relative_path)
    if candidate_path.is_absolute():
        raise ValueError("repository paths must be relative")
    resolved_root = root.resolve(strict=True)
    resolved_candidate = (resolved_root / candidate_path).resolve(strict=must_exist)
    try:
        # 必须在解析符号链接后检查相对关系，否则仓库内 symlink 可逃逸到工作区外。
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("repository path escapes the checked-out source tree") from exc
    return resolved_candidate


def canonical_snapshot(snapshot: Mapping[str, Any]) -> tuple[bytes, str]:
    """编码快照并返回内容与 SHA-256，供上传和恢复时校验一致性。"""
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return encoded, hashlib.sha256(encoded).hexdigest()


def _prepare_destination(destination: Path) -> None:
    """确保检出目录为空或不存在，避免旧源码污染本次构建。"""
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("checkout destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)


def _validated_repo_url(value: str) -> str:
    """限制仓库地址为无内嵌凭据的绝对 HTTP(S) URL。"""
    if value != value.strip() or any(character in value for character in "\x00\r\n"):
        raise ValueError("repo_url is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("repo_url is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("repo_url must be an absolute HTTP(S) URL without embedded credentials")
    return value
