"""检查核心源码与运行配置是否包含符合仓库规范的文件头功能说明。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _files(pattern: str) -> list[Path]:
    return sorted(path for path in ROOT.glob(pattern) if path.is_file())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _python_has_docstring(path: Path) -> bool:
    return _read(path).lstrip("\ufeff").startswith(('"""', "'''"))


def _vue_has_comment(path: Path) -> bool:
    return _read(path).lstrip("\ufeff\r\n \t").startswith("<!--")


def _block_comment_at_start(path: Path) -> bool:
    return _read(path).lstrip("\ufeff\r\n \t").startswith("/*")


def _typescript_has_comment(path: Path) -> bool:
    content = _read(path).lstrip("\ufeff")
    if content.startswith("/// <reference"):
        lines = content.splitlines()
        content = "\n".join(lines[1:]).lstrip()
    return content.startswith("/*")


def _shell_has_comment(path: Path) -> bool:
    lines = _read(path).splitlines()
    return len(lines) >= 2 and lines[0].startswith("#!") and lines[1].startswith("# ")


def _hash_comment_at_start(path: Path, *, allow_syntax_directive: bool = False) -> bool:
    lines = _read(path).splitlines()
    if not lines:
        return False
    index = 1 if allow_syntax_directive and lines[0].startswith("# syntax=") else 0
    return len(lines) > index and lines[index].startswith("# ")


def _systemd_has_comment(path: Path) -> bool:
    return _read(path).lstrip("\ufeff\r\n \t").startswith("# ")


def _migration_has_docstring(path: Path) -> bool:
    return bool(re.match(r'^\s*(?:"""|\'\'\')', _read(path)))


def main() -> int:
    checks: list[tuple[Path, bool]] = []
    checks.extend((path, _python_has_docstring(path)) for path in _files("backend/src/devops/**/*.py"))
    checks.extend((path, _migration_has_docstring(path)) for path in _files("backend/alembic/**/*.py"))
    checks.extend((path, _vue_has_comment(path)) for path in _files("frontend/src/**/*.vue"))
    checks.extend((path, _typescript_has_comment(path)) for path in _files("frontend/src/**/*.ts"))
    checks.extend((path, _block_comment_at_start(path)) for path in _files("frontend/src/**/*.css"))
    checks.append((ROOT / "frontend/vite.config.ts", _typescript_has_comment(ROOT / "frontend/vite.config.ts")))
    checks.extend(
        (path, _hash_comment_at_start(path))
        for path in (
            ROOT / "backend/pyproject.toml",
            ROOT / "backend/alembic.ini",
            ROOT / "pnpm-workspace.yaml",
        )
    )
    checks.append((ROOT / "infra/systemd/install.sh", _shell_has_comment(ROOT / "infra/systemd/install.sh")))
    checks.extend(
        (path, _hash_comment_at_start(path))
        for path in (
            ROOT / "docker-compose.yml",
            ROOT / ".github/workflows/ci.yml",
            ROOT / ".env.example",
            ROOT / "backend/.env.example",
            ROOT / "frontend/.env.example",
            ROOT / "infra/systemd/devops.env.example",
            ROOT / "templates/compose/compose.yaml",
        )
    )
    checks.extend(
        (path, _hash_comment_at_start(path, allow_syntax_directive=True))
        for path in (
            ROOT / "infra/docker/api.Dockerfile",
            ROOT / "infra/docker/runner.Dockerfile",
            ROOT / "templates/java-maven/Dockerfile",
            ROOT / "templates/java-gradle/Dockerfile",
            ROOT / "templates/node/Dockerfile",
            ROOT / "templates/python/Dockerfile",
        )
    )
    checks.extend(
        (path, _systemd_has_comment(path))
        for path in (
            ROOT / "infra/systemd/devops-api.service",
            ROOT / "infra/systemd/devops-runner.service",
        )
    )

    missing = [path.relative_to(ROOT) for path, valid in checks if not valid]
    if missing:
        print("以下核心文件缺少文件头功能说明：", file=sys.stderr)
        for path in missing:
            print(f"- {path.as_posix()}", file=sys.stderr)
        return 1
    print(f"核心文件头检查通过：{len(checks)} 个文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
