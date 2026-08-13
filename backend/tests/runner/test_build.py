from __future__ import annotations

import asyncio
import json
from pathlib import Path

from devops.runner.build import BuildRequest, BuildxBuilder
from devops.runner.credentials import RegistryCredentials
from devops.runner.process import CommandResult, CommandSpec


class FakeCommandRunner:
    def __init__(self) -> None:
        self.specs: list[CommandSpec] = []
        self.docker_config: Path | None = None
        self.docker_config_content = ""

    async def run(self, spec: CommandSpec, **_: object) -> CommandResult:
        self.specs.append(spec)
        if spec.env and spec.env.get("DOCKER_CONFIG"):
            self.docker_config = Path(spec.env["DOCKER_CONFIG"])
            self.docker_config_content = (
                self.docker_config / "config.json"
            ).read_text(encoding="utf-8")
        metadata_index = spec.argv.index("--metadata-file")
        metadata_path = Path(spec.argv[metadata_index + 1])
        await asyncio.to_thread(
            metadata_path.write_text,
            json.dumps({"containerimage.digest": "sha256:" + "a" * 64}),
            encoding="utf-8",
        )
        return CommandResult(
            argv=spec.argv,
            returncode=0,
            stdout=b"",
            stderr=b"",
            duration_seconds=0.1,
        )


async def test_buildx_build_uses_metadata_digest_and_immutable_ref(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    commands = FakeCommandRunner()
    builder = BuildxBuilder(commands)  # type: ignore[arg-type]

    artifact = await builder.build_and_push(
        BuildRequest(
            source_root=tmp_path,
            dockerfile_path="Dockerfile",
            context_path=".",
            image_ref="registry.example.test/team/app:abc123",
            build_args={"VERSION": "1"},
            registry_credentials=RegistryCredentials(
                username="builder",
                password="registry-secret",
                endpoint="registry.example.test",
            ),
        )
    )

    assert artifact.digest == "sha256:" + "a" * 64
    assert artifact.immutable_ref == "registry.example.test/team/app@" + artifact.digest
    argv = commands.specs[0].argv
    assert argv[:3] == ("docker", "buildx", "build")
    assert "--push" in argv
    assert "VERSION=1" in argv
    assert commands.docker_config is not None
    assert "registry.example.test" in commands.docker_config_content
    assert "registry-secret" not in commands.docker_config_content
    assert not commands.docker_config.exists()
